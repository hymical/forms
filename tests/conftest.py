"""
shared test fixtures

Tests build their own application instances so that limits can be lowered to
values that are cheap to exercise, and so that a developer's local environment
can never change a test's outcome. Each application gets its own in-memory
SQLite database, which starts empty and disappears when the test ends.

Each application also gets a management API key, written straight into its
database the way the operator CLI writes one. Clients send it by default so that
the tests which are about ingestion stay about ingestion; a client built with
``authenticate=False`` genuinely sends no ``Authorization`` header, which is what
the public-route tests need.
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

from hymical_forms import apikeys, models, storage
from hymical_forms.app import create_app
from hymical_forms.config import Settings
from hymical_forms.delivery import create_webhook_client
from hymical_forms.ingestion import new_submission_id
from hymical_forms.models import utcnow
from hymical_forms.schema import create_all
from hymical_forms.webhooks import DeliveryState, new_webhook_delivery_id
from hymical_forms.worker import process_batch
from webhook_server import WebhookRecorder

URLENCODED_HEADERS = {"content-type": "application/x-www-form-urlencoded"}

AUTHORIZATION_HEADER = "Authorization"
DEFAULT_KEY_NAME = "test-operator"

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
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    register an endpoint through the management API, failing loudly if it does not take
    :param client: the client whose application should hold the endpoint
    :param endpoint_id: the public identifier to register
    :param name: human-readable label for the endpoint
    :param is_active: whether the endpoint should accept submissions
    :param webhook_url: destination to deliver submissions to, or None for no webhook
    :param api_key: management key to authenticate with, or None to use the client's own
    :returns: the created endpoint as the API returned it
    """
    body: dict[str, Any] = {"id": endpoint_id, "name": name, "is_active": is_active}
    if webhook_url is not None:
        body["webhook_url"] = webhook_url
    headers = bearer(api_key) if api_key is not None else None
    response = client.post("/endpoints", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def seed_submission(
    client: TestClient,
    *,
    received_at: datetime,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
    fields: dict[str, list[str]] | None = None,
    idempotency_key: str | None = None,
    delivery_state: DeliveryState | None = None,
    attempts: int = 0,
) -> str:
    """
    write a submission straight into a client's database, at a chosen moment
    :param client: the client whose application database should hold it
    :param received_at: the instant the submission should claim it was accepted
    :param endpoint_id: the endpoint it belongs to
    :param fields: the submitted values, defaulting to one email field
    :param idempotency_key: the key it was sent with, or None if it was sent without one
    :param delivery_state: the state of the delivery it owes, or None to owe none
    :param attempts: how many requests have been made for that delivery
    :returns: the submission identifier
    """
    # Ingestion decides ``received_at`` from the clock, so anything about time
    # ranges or retention has to write the row rather than post to the endpoint.
    # It goes in through the models the API reads back, not through raw SQL, so a
    # seeded submission is indistinguishable from a submitted one.
    submission_id = new_submission_id()
    terminal = delivery_state in (DeliveryState.DELIVERED, DeliveryState.FAILED)
    with open_session(client) as session:
        session.add(
            models.Submission(
                id=submission_id,
                endpoint_id=endpoint_id,
                received_at=received_at,
                fields=fields if fields is not None else {"email": ["dev@example.com"]},
                idempotency_key=idempotency_key,
                # Paired with the key by a check constraint, and never read back
                # by anything these tests assert on.
                payload_fingerprint="f" * 64 if idempotency_key is not None else None,
            )
        )
        if delivery_state is not None:
            session.add(
                models.WebhookDelivery(
                    id=new_webhook_delivery_id(),
                    submission_id=submission_id,
                    endpoint_id=endpoint_id,
                    destination_url="https://example.invalid/hook",
                    signing_secret="whsec_" + "a" * 64,
                    state=delivery_state,
                    attempts=attempts,
                    cycle_attempts=attempts,
                    next_attempt_at=received_at,
                    created_at=received_at,
                    completed_at=received_at if terminal else None,
                )
            )
        session.commit()
    return submission_id


def bearer(api_key: str) -> dict[str, str]:
    """
    build the authorization header a management request carries
    :param api_key: the full management key
    :returns: headers authenticating as that key
    """
    return {AUTHORIZATION_HEADER: f"Bearer {api_key}"}


def issue_management_key(client: TestClient, *, name: str = DEFAULT_KEY_NAME) -> str:
    """
    mint a management key into the client's database, the way the operator CLI does
    :param client: the client whose application database should hold the key
    :param name: human-readable label for the key
    :returns: the full credential, which exists only here and in the caller
    """
    # Written through the same domain and storage functions the CLI uses, rather
    # than through a fixture-only shortcut, so what the tests authenticate with
    # is what an operator would actually be holding.
    generated = apikeys.new_management_key()
    with open_session(client) as session:
        storage.create_management_key(
            session,
            key_id=generated.id,
            name=name,
            display_prefix=generated.display_prefix,
            key_digest=generated.digest,
            now=utcnow(),
        )
        session.commit()
    return generated.key


def management_key(client: TestClient) -> str:
    """
    read the management key an authenticated client sends
    :param client: a client built with authentication enabled
    :returns: the full credential the fixture issued for it
    """
    return client.headers[AUTHORIZATION_HEADER].removeprefix("Bearer ")


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

        def factory(
            *, seed_endpoint: bool = True, authenticate: bool = True, **overrides: Any
        ) -> TestClient:
            """
            build a client for an app configured with the given overrides
            :param seed_endpoint: whether to register the default endpoint first
            :param authenticate: whether the client should send its management key by default
            :param overrides: setting values to replace the built-in defaults
            :returns: a test client closed when the fixture tears down
            """
            # The schema is built from the models and stamped, rather than
            # migrated, because replaying migrations for each of a couple of
            # hundred tests would cost far more than it proves. That the two
            # produce the same schema is asserted once, against PostgreSQL, in
            # the integration suite.
            app = create_app(build_settings(**overrides))
            create_all(app.state.engine)
            client = stack.enter_context(TestClient(app))

            # A key always exists, so seeding an endpoint works either way. Only
            # whether the client sends it by default depends on ``authenticate``,
            # which is what lets a test prove a route is reachable without one.
            key = issue_management_key(client)
            if authenticate:
                client.headers[AUTHORIZATION_HEADER] = f"Bearer {key}"
            if seed_endpoint:
                create_endpoint(client, api_key=key)
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
