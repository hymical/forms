"""
shared test fixtures

Tests build their own application instances so that limits can be lowered to
values that are cheap to exercise, and so that a developer's local environment
can never change a test's outcome. Each application gets its own in-memory
SQLite database, which starts empty and disappears when the test ends.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms.app import create_app
from hymical_forms.config import Settings

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
) -> dict[str, Any]:
    """
    register an endpoint through the public API, failing loudly if it does not take
    :param client: the client whose application should hold the endpoint
    :param endpoint_id: the public identifier to register
    :param name: human-readable label for the endpoint
    :param is_active: whether the endpoint should accept submissions
    :returns: the created endpoint as the API returned it
    """
    response = client.post(
        "/endpoints",
        json={"id": endpoint_id, "name": name, "is_active": is_active},
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


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
