"""
shared test fixtures

Tests build their own application instances so that limits can be lowered to
values that are cheap to exercise, and so that a developer's local environment
can never change a test's outcome. Each application gets its own in-memory
SQLite database, which starts empty and disappears when the test ends.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms.app import create_app
from hymical_forms.config import Settings
from hymical_forms.delivery import create_webhook_client
from hymical_forms.worker import process_batch
from webhook_server import WebhookRecorder

URLENCODED_HEADERS = {"content-type": "application/x-www-form-urlencoded"}

DEFAULT_ENDPOINT_ID = "contact-form"
DEFAULT_ENDPOINT_NAME = "Contact form"

# In-memory, so nothing survives a test and nothing touches the working tree.
TEST_DATABASE_URL = "sqlite://"

ClientFactory = Callable[..., TestClient]


class IsolatedSettings(Settings):
    """
    settings that ignore a local ``.env``, so a developer's file cannot skew a run
    """

    model_config = SettingsConfigDict(env_file=None)


@pytest.fixture(autouse=True)
def _ignore_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    hide any ``FORMS_*`` variables the developer happens to have exported
    :param monkeypatch: pytest fixture used to remove the variables for one test
    """
    for name in list(os.environ):
        if name.startswith("FORMS_"):
            monkeypatch.delenv(name)


def build_settings(**overrides: Any) -> Settings:
    """
    build settings from defaults and explicit overrides only
    :param overrides: setting values to replace the built-in defaults
    :returns: settings that ignore the ambient environment
    """
    overrides.setdefault("database_url", TEST_DATABASE_URL)
    return IsolatedSettings(**overrides)


def create_endpoint(
    client: TestClient,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
    *,
    name: str = DEFAULT_ENDPOINT_NAME,
    is_active: bool = True,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """
    register an endpoint through the public API, failing loudly if it does not take
    :param client: the client whose application should hold the endpoint
    :param endpoint_id: the public identifier to register
    :param name: human-readable label for the endpoint
    :param is_active: whether the endpoint should accept submissions
    :param webhook_url: destination to deliver submissions to, or None for no webhook
    :returns: the created endpoint as the API returned it
    """
    body: dict[str, Any] = {"id": endpoint_id, "name": name, "is_active": is_active}
    if webhook_url is not None:
        body["webhook_url"] = webhook_url
    response = client.post("/endpoints", json=body)
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def app_settings(client: TestClient) -> Settings:
    """
    read the settings the client's application was built with
    :param client: the client whose application should be inspected
    :returns: that application's settings
    """
    return cast(Settings, cast(FastAPI, client.app).state.settings)


def work_once(client: TestClient, *, now: datetime | None = None) -> int:
    """
    run one worker batch against the client's database, as a separate worker would
    :param client: the client whose application database holds the delivery queue
    :param now: the instant the worker should treat as current, defaulting to real time
    :returns: how many deliveries were attempted
    """
    # Deliberately not started through the API process. The worker gets its own
    # session and its own outbound client, the way a separate process would.
    settings = app_settings(client)
    moment = now if now is not None else datetime.now(UTC)

    async def run() -> int:
        webhook_client = create_webhook_client(settings)
        try:
            with open_session(client) as session:
                return await process_batch(session, webhook_client, settings, now=moment)
        finally:
            await webhook_client.aclose()

    return asyncio.run(run())


def open_session(client: TestClient) -> Session:
    """
    open a session against the database behind a client, for asserting on rows
    :param client: the client whose application database should be inspected
    :returns: a new session the caller is responsible for closing
    """
    app = cast(FastAPI, client.app)
    factory: sessionmaker[Session] = app.state.session_factory
    return factory()


@pytest.fixture
def webhook() -> Iterator[WebhookRecorder]:
    """
    provide a running local server that records the webhooks delivered to it
    :returns: an iterator yielding the recorder, stopped when the test ends
    """
    with WebhookRecorder() as recorder:
        yield recorder


@pytest.fixture
def make_client() -> Iterator[ClientFactory]:
    """
    provide a factory for clients bound to an app with the given setting overrides
    :returns: a factory that accepts setting overrides and returns a test client
    """
    with ExitStack() as stack:

        def factory(*, seed_endpoint: bool = True, **overrides: Any) -> TestClient:
            """
            build a client for an app configured with the given overrides
            :param seed_endpoint: whether to register the default endpoint first
            :param overrides: setting values to replace the built-in defaults
            :returns: a test client closed when the fixture tears down
            """
            client = stack.enter_context(TestClient(create_app(build_settings(**overrides))))
            if seed_endpoint:
                create_endpoint(client)
            return client

        yield factory


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    """
    provide a client on default settings whose app already holds the default endpoint
    :param make_client: factory for clients bound to a configured app
    :returns: a test client that can ingest submissions straight away
    """
    return make_client()


@pytest.fixture
def empty_client(make_client: ClientFactory) -> TestClient:
    """
    provide a client on default settings whose app holds no endpoints at all
    :param make_client: factory for clients bound to a configured app
    :returns: a test client with an empty database
    """
    return make_client(seed_endpoint=False)
