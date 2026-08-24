"""
the management authentication boundary on ``POST /endpoints``, and what stays public
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import (
    ClientFactory,
    bearer,
    create_endpoint,
    issue_management_key,
    management_key,
    open_session,
    work_once,
)
from hymical_forms import apikeys, models, storage
from hymical_forms.models import utcnow
from webhook_server import WebhookRecorder

CREATE_BODY = {"id": "contact-form", "name": "Contact form"}

# --- what authenticates, and what does not -----------------------------------


def test_a_valid_key_creates_an_endpoint(empty_client: TestClient) -> None:
    response = empty_client.post("/endpoints", json=CREATE_BODY)

    assert response.status_code == 201
    assert response.json()["id"] == "contact-form"


def test_creating_an_endpoint_without_credentials_is_refused(make_client: ClientFactory) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.post("/endpoints", json=CREATE_BODY)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_a_refused_request_creates_nothing(make_client: ClientFactory) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    client.post("/endpoints", json=CREATE_BODY)

    with open_session(client) as session:
        assert list(session.scalars(select(models.Endpoint))) == []


@pytest.mark.parametrize(
    ("description", "value"),
    [
        ("empty", ""),
        ("the scheme alone", "Bearer"),
        ("the scheme with nothing after it", "Bearer "),
        ("no scheme at all", "hym_live_abcdefgh"),
        ("basic auth", "Basic aHltOmxpdmU="),
        ("a different scheme", "Token hym_live_abcdefgh"),
        ("nonsense", "!!!"),
    ],
)
def test_an_unusable_authorization_header_is_refused(
    make_client: ClientFactory, description: str, value: str
) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.post("/endpoints", json=CREATE_BODY, headers={"Authorization": value})

    assert response.status_code == 401, description
    # No bearer credential arrived, so the caller is told how to send one rather
    # than that a key they never supplied was checked and refused.
    assert response.json()["error"]["code"] == "authentication_required", description


@pytest.mark.parametrize(
    ("description", "key"),
    [
        ("malformed", "not-a-hymical-key"),
        ("the right prefix but too short", apikeys.MANAGEMENT_KEY_PREFIX + "abc"),
        ("well formed but unknown", apikeys.new_management_key().key),
    ],
)
def test_a_bearer_credential_that_does_not_authenticate_is_refused(
    make_client: ClientFactory, description: str, key: str
) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.post("/endpoints", json=CREATE_BODY, headers=bearer(key))

    assert response.status_code == 401, description
    # Malformed, unknown and revoked deliberately answer identically, so a
    # guesser learns nothing about which of their guesses was closer.
    assert response.json()["error"]["code"] == "invalid_api_key", description


def test_a_revoked_key_stops_authenticating_immediately(empty_client: TestClient) -> None:
    """
    revocation must take effect on the next request, with nothing caching the key
    :param empty_client: test client whose app holds no endpoints
    """
    key = management_key(empty_client)
    assert empty_client.post("/endpoints", json=CREATE_BODY).status_code == 201

    with open_session(empty_client) as session:
        stored = storage.find_management_key_by_digest(session, apikeys.digest_key(key))
        assert stored is not None
        storage.revoke_management_key(session, stored.id, now=utcnow())

    response = empty_client.post("/endpoints", json={"id": "waitlist", "name": "Waitlist"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_one_application_accepts_more_than_one_key(empty_client: TestClient) -> None:
    second = issue_management_key(empty_client, name="second-operator")

    response = empty_client.post("/endpoints", json=CREATE_BODY, headers=bearer(second))

    assert response.status_code == 201


def test_revoking_one_key_leaves_another_working(empty_client: TestClient) -> None:
    first = management_key(empty_client)
    second = issue_management_key(empty_client, name="second-operator")

    with open_session(empty_client) as session:
        stored = storage.find_management_key_by_digest(session, apikeys.digest_key(first))
        assert stored is not None
        storage.revoke_management_key(session, stored.id, now=utcnow())

    response = empty_client.post("/endpoints", json=CREATE_BODY, headers=bearer(second))

    assert response.status_code == 201


# --- the shape of a refusal --------------------------------------------------


@pytest.mark.parametrize(
    ("description", "headers"),
    [
        ("no credentials", {}),
        ("an invalid key", {"Authorization": "Bearer " + apikeys.new_management_key().key}),
    ],
)
def test_a_refusal_carries_the_bearer_challenge(
    make_client: ClientFactory, description: str, headers: dict[str, str]
) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.post("/endpoints", json=CREATE_BODY, headers=headers)

    assert response.headers["www-authenticate"] == "Bearer", description


def test_a_refusal_uses_the_shared_error_envelope(make_client: ClientFactory) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.post("/endpoints", json=CREATE_BODY)

    error = response.json()["error"]
    assert set(error) <= {"code", "message", "details"}
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)


def test_a_refusal_never_echoes_the_supplied_credential(make_client: ClientFactory) -> None:
    """
    a credential reflected into an error body would end up in somebody's log
    :param make_client: factory for clients bound to a configured app
    """
    client = make_client(seed_endpoint=False, authenticate=False)
    key = apikeys.new_management_key().key

    response = client.post("/endpoints", json=CREATE_BODY, headers=bearer(key))

    assert key not in response.text
    assert key.removeprefix(apikeys.MANAGEMENT_KEY_PREFIX) not in response.text


def test_a_successful_request_does_not_log_the_credential(
    empty_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    key = management_key(empty_client)

    with caplog.at_level(logging.DEBUG):
        empty_client.post("/endpoints", json=CREATE_BODY)

    assert caplog.text != ""
    assert key not in caplog.text
    assert key.removeprefix(apikeys.MANAGEMENT_KEY_PREFIX) not in caplog.text


# --- authentication does not change what endpoint creation does --------------


def test_a_duplicate_endpoint_still_conflicts_when_authenticated(
    empty_client: TestClient,
) -> None:
    create_endpoint(empty_client, "contact-form")

    response = empty_client.post("/endpoints", json={"id": "contact-form", "name": "Another"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "endpoint_already_exists"


def test_the_endpoint_records_nothing_about_the_key_that_made_it(
    empty_client: TestClient,
) -> None:
    """
    a management key administers the service, it does not own what it configures
    :param empty_client: test client whose app holds no endpoints
    """
    create_endpoint(empty_client, "contact-form")

    columns = {column.name for column in models.Endpoint.__table__.c}

    assert "api_key_id" not in columns
    assert not any("key" in name for name in columns - {"webhook_secret"})


def test_authenticating_records_when_the_key_was_last_used(empty_client: TestClient) -> None:
    key = management_key(empty_client)

    with open_session(empty_client) as session:
        stored = storage.find_management_key_by_digest(session, apikeys.digest_key(key))
        assert stored is not None
        assert stored.last_used_at is None

    empty_client.post("/endpoints", json=CREATE_BODY)

    with open_session(empty_client) as session:
        stored = storage.find_management_key_by_digest(session, apikeys.digest_key(key))
        assert stored is not None
        assert stored.last_used_at is not None


# --- routes that must stay public --------------------------------------------


def test_health_needs_no_credentials(make_client: ClientFactory) -> None:
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.get("/health")

    assert response.status_code == 200
    assert "authorization" not in client.headers
    assert response.json()["status"] == "ok"


def test_form_ingestion_needs_no_credentials(make_client: ClientFactory) -> None:
    """
    the ingestion URL sits in somebody's HTML form, so it cannot require a header
    :param make_client: factory for clients bound to a configured app
    """
    client = make_client(authenticate=False)

    response = client.post("/f/contact-form", data={"email": "dev@example.com"})

    assert "authorization" not in client.headers
    assert response.status_code == 202
    assert response.json()["endpoint_id"] == "contact-form"


def test_an_unknown_endpoint_still_answers_404_without_credentials(
    make_client: ClientFactory,
) -> None:
    # A 401 here would tell a browser form to ask for credentials it can never
    # have, so the public path has to keep answering the way it always did.
    client = make_client(seed_endpoint=False, authenticate=False)

    response = client.post("/f/nothing-here", data={"email": "dev@example.com"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "endpoint_not_found"


def test_a_public_submission_still_queues_a_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(
        seed_endpoint=False, authenticate=False, allow_private_webhook_targets=True
    )
    key = issue_management_key(client, name="setup")
    create_endpoint(client, "contact-form", webhook_url=webhook.url, api_key=key)

    accepted = client.post("/f/contact-form", data={"email": "dev@example.com"})

    assert accepted.status_code == 202
    assert accepted.json()["delivery"]["queued"] is True
    assert work_once(client) == 1
    assert len(webhook.received) == 1


def test_a_management_authorization_header_never_reaches_a_webhook(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a credential forwarded to a destination would hand it full management access
    :param make_client: factory for clients bound to a configured app
    :param webhook: a local server recording the deliveries it is sent
    """
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    key = management_key(client)
    create_endpoint(client, "contact-form", webhook_url=webhook.url)

    # Submitted with the management credential attached, which the public route
    # has no use for. It must go no further than this process.
    client.post("/f/contact-form", data={"email": "dev@example.com"}, headers=bearer(key))
    work_once(client)

    assert len(webhook.received) == 1
    delivered = webhook.received[0]
    assert "authorization" not in delivered.headers
    assert key not in str(delivered.headers)
    assert key.encode() not in delivered.body
