"""
webhook delivery: configuration, the signed payload, and what one attempt records

Every test here points the service at a real local server or at a closed local
port. Nothing reaches the internet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from conftest import ClientFactory, create_endpoint, open_session
from hymical_forms import models, storage
from webhook_server import WebhookRecorder, unused_local_url

ENDPOINT = "/f/contact-form"
KEY = "b8f1c2d4e5a67890b8f1c2d4e5a67890"


def hooked_client(
    make_client: ClientFactory, url: str, **overrides: Any
) -> tuple[TestClient, dict[str, Any]]:
    """
    build a client whose default endpoint delivers to a given destination
    :param make_client: factory for clients bound to a configured app
    :param url: the webhook destination to configure
    :param overrides: extra setting overrides for the application
    :returns: the client and the created endpoint as the API returned it
    """
    # Loopback destinations are refused unless this is on, which is exactly the
    # protection being relied upon in the SSRF tests further down.
    overrides.setdefault("allow_private_webhook_targets", True)
    client = make_client(seed_endpoint=False, **overrides)
    endpoint = create_endpoint(client, webhook_url=url)
    return client, endpoint


def attempts(client: TestClient) -> list[models.DeliveryAttempt]:
    """
    read every persisted delivery attempt behind a client
    :param client: the client whose application database should be inspected
    :returns: the attempt rows
    """
    with open_session(client) as session:
        return list(session.scalars(select(models.DeliveryAttempt)))


# --- configuration -----------------------------------------------------------


def test_an_endpoint_can_be_created_with_a_webhook(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    _, endpoint = hooked_client(make_client, webhook.url)

    assert endpoint["webhook_url"] == webhook.url
    assert endpoint["webhook_secret"].startswith("whsec_")


def test_the_webhook_configuration_is_persisted(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = hooked_client(make_client, webhook.url)

    with open_session(client) as session:
        row = session.get(models.Endpoint, "contact-form")
        assert row is not None
        assert row.webhook_url == webhook.url
        assert row.webhook_secret == endpoint["webhook_secret"]


def test_an_endpoint_without_a_webhook_reports_none(client: TestClient) -> None:
    with open_session(client) as session:
        row = session.get(models.Endpoint, "contact-form")
        assert row is not None
        assert row.webhook_url is None
        assert row.webhook_secret is None


def test_each_endpoint_gets_its_own_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)

    first = create_endpoint(client, "form-one", webhook_url=webhook.url)
    second = create_endpoint(client, "form-two", webhook_url=webhook.url)

    assert first["webhook_secret"] != second["webhook_secret"]


@pytest.mark.parametrize(
    ("description", "url"),
    [
        ("a file URL", "file:///etc/passwd"),
        ("an ftp URL", "ftp://example.com/hook"),
        ("a javascript URL", "javascript:alert(1)"),
        ("a scheme-less string", "example.com/hook"),
        ("empty", ""),
        ("no host", "https:///hook"),
        ("nonsense", "not a url at all"),
    ],
)
def test_rejects_an_unusable_webhook_url(
    make_client: ClientFactory, description: str, url: str
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)

    response = client.post(
        "/endpoints", json={"id": "contact-form", "name": "C", "webhook_url": url}
    )

    assert response.status_code == 422, description
    assert response.json()["error"]["code"] == "invalid_webhook_url"


@pytest.mark.parametrize(
    ("description", "url"),
    [
        ("localhost", "http://localhost:9000/hook"),
        ("a localhost subdomain", "http://api.localhost/hook"),
        ("ipv4 loopback", "http://127.0.0.1:9000/hook"),
        ("ipv6 loopback", "http://[::1]:9000/hook"),
        ("an ipv4-mapped ipv6 loopback", "http://[::ffff:127.0.0.1]/hook"),
        ("a private range", "http://10.1.2.3/hook"),
        ("another private range", "http://192.168.0.5/hook"),
        ("link-local metadata", "http://169.254.169.254/latest/meta-data/"),
        ("the unspecified address", "http://0.0.0.0/hook"),
    ],
)
def test_rejects_an_internal_webhook_target(
    make_client: ClientFactory, description: str, url: str
) -> None:
    """
    obvious internal destinations are refused unless a development setting allows them
    :param make_client: factory for clients bound to a configured app
    :param description: what makes the destination internal
    :param url: the destination under test
    """
    client = make_client(seed_endpoint=False)

    response = client.post(
        "/endpoints", json={"id": "contact-form", "name": "C", "webhook_url": url}
    )

    assert response.status_code == 422, description
    assert response.json()["error"]["code"] == "invalid_webhook_url"


def test_a_rejected_webhook_url_creates_no_endpoint(make_client: ClientFactory) -> None:
    client = make_client(seed_endpoint=False)

    client.post(
        "/endpoints",
        json={"id": "contact-form", "name": "C", "webhook_url": "http://127.0.0.1/hook"},
    )

    with open_session(client) as session:
        assert list(session.scalars(select(models.Endpoint))) == []


# --- successful delivery -----------------------------------------------------


def test_a_submission_is_delivered_once(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["delivery"] == {"attempted": True, "outcome": "succeeded"}
    assert len(webhook.received) == 1


def test_the_payload_matches_the_documented_contract(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(
        ENDPOINT, data={"email": "dev@example.com", "topics": ["billing", "api"]}
    )

    body = response.json()
    payload = json.loads(webhook.received[0].body)
    assert payload == {
        "type": "submission.received",
        "submission": {
            "id": body["submission_id"],
            "endpoint_id": "contact-form",
            "received_at": body["received_at"],
            "fields": {"email": ["dev@example.com"], "topics": ["billing", "api"]},
        },
    }
    assert webhook.received[0].headers["content-type"] == "application/json"


def test_repeated_field_values_survive_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"topic": ["billing", "api", "billing"]})

    payload = json.loads(webhook.received[0].body)
    assert payload["submission"]["fields"]["topic"] == ["billing", "api", "billing"]


def test_the_signature_verifies_against_the_exact_bytes_sent(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a receiver following the README must be able to verify what actually arrived
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, endpoint = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"email": "dev@example.com", "note": "héllo, wörld"})

    delivered = webhook.received[0]
    expected = hmac.new(
        endpoint["webhook_secret"].encode("utf-8"), delivered.body, hashlib.sha256
    ).hexdigest()
    assert delivered.headers["hymical-signature"] == f"v1={expected}"


def test_a_different_secret_does_not_verify(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"email": "dev@example.com"})

    delivered = webhook.received[0]
    wrong = hmac.new(b"whsec_not-the-secret", delivered.body, hashlib.sha256).hexdigest()
    assert delivered.headers["hymical-signature"] != f"v1={wrong}"


def test_a_successful_attempt_is_recorded(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)
    before = datetime.now(UTC)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    after = datetime.now(UTC)

    recorded = attempts(client)
    assert len(recorded) == 1
    attempt = recorded[0]
    assert attempt.submission_id == response.json()["submission_id"]
    assert attempt.destination_url == webhook.url
    assert attempt.outcome == "succeeded"
    assert attempt.response_status == 200
    assert attempt.error is None
    assert before <= attempt.attempted_at <= after


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_any_2xx_counts_as_delivered(
    make_client: ClientFactory, webhook: WebhookRecorder, status: int
) -> None:
    webhook.status = status
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.json()["delivery"]["outcome"] == "succeeded"
    assert attempts(client)[0].response_status == status


# --- failures ----------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 400, 404, 410, 500, 503])
def test_a_non_2xx_response_is_a_failed_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder, status: int
) -> None:
    """
    redirects are not followed, so a 3xx is a failure like any other non-2xx
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    :param status: the status the destination answers with
    """
    webhook.status = status
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["delivery"] == {"attempted": True, "outcome": "http_error"}
    attempt = attempts(client)[0]
    assert attempt.outcome == "http_error"
    assert attempt.response_status == status


def test_a_failed_delivery_keeps_the_submission(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 500
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    with open_session(client) as session:
        stored = list(session.scalars(select(models.Submission)))
    assert len(stored) == 1
    assert stored[0].id == response.json()["submission_id"]


def test_a_refused_connection_is_recorded_as_a_network_error(
    make_client: ClientFactory,
) -> None:
    client, _ = hooked_client(make_client, unused_local_url())

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["delivery"]["outcome"] == "network_error"
    attempt = attempts(client)[0]
    assert attempt.outcome == "network_error"
    assert attempt.response_status is None
    assert attempt.error is not None


def test_a_slow_destination_times_out_without_hanging(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    the read timeout bounds the request, so a silent destination cannot stall ingestion
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    webhook.delay_seconds = 2.0
    client, _ = hooked_client(make_client, webhook.url, webhook_read_timeout_seconds=0.25)

    started = datetime.now(UTC)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    elapsed = (datetime.now(UTC) - started).total_seconds()

    assert response.status_code == 202
    assert response.json()["delivery"]["outcome"] == "timeout"
    assert elapsed < webhook.delay_seconds, "the request should return before the destination does"
    assert attempts(client)[0].outcome == "timeout"


def test_a_timed_out_delivery_keeps_the_submission(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.delay_seconds = 2.0
    client, _ = hooked_client(make_client, webhook.url, webhook_read_timeout_seconds=0.25)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    with open_session(client) as session:
        stored = list(session.scalars(select(models.Submission)))
    assert [row.id for row in stored] == [response.json()["submission_id"]]


def test_a_stored_failure_message_is_bounded(make_client: ClientFactory) -> None:
    client, _ = hooked_client(make_client, unused_local_url())

    client.post(ENDPOINT, data={"email": "dev@example.com"})

    error = attempts(client)[0].error
    assert error is not None
    assert len(error) <= 500


def test_a_failure_to_record_the_attempt_still_acknowledges_the_submission(
    make_client: ClientFactory, webhook: WebhookRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    the submission is durable and the webhook already went out, so 202 is the honest answer
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    :param monkeypatch: pytest fixture used to break the bookkeeping write
    """
    # Answering with an error here would tell the client its form was lost, which
    # is untrue, and would invite a retry that delivers the webhook a second time.
    client, _ = hooked_client(make_client, webhook.url)

    def explode(*args: object, **kwargs: object) -> None:
        raise OperationalError("INSERT INTO delivery_attempts", {}, Exception("disk full"))

    monkeypatch.setattr(storage, "record_delivery_attempt", explode)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    monkeypatch.undo()

    assert response.status_code == 202
    assert response.json()["delivery"] == {"attempted": True, "outcome": "succeeded"}
    assert len(webhook.received) == 1
    with open_session(client) as session:
        stored = list(session.scalars(select(models.Submission)))
    assert [row.id for row in stored] == [response.json()["submission_id"]]
    assert attempts(client) == []


# --- no webhook configured ---------------------------------------------------


def test_without_a_webhook_nothing_is_attempted(client: TestClient) -> None:
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["delivery"] == {"attempted": False, "outcome": None}
    assert attempts(client) == []


# --- idempotency interaction -------------------------------------------------


def test_a_replay_does_not_deliver_again(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a client retrying a lost response must not cause duplicate downstream processing
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}

    first = client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)
    second = client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    assert first.status_code == second.status_code == 202
    assert second.json()["submission_id"] == first.json()["submission_id"]
    assert second.json()["idempotent_replay"] is True
    assert second.json()["delivery"] == {"attempted": False, "outcome": None}
    assert len(webhook.received) == 1
    assert len(attempts(client)) == 1


def test_many_replays_still_deliver_once(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}

    for _ in range(4):
        client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    assert len(webhook.received) == 1
    assert len(attempts(client)) == 1


def test_an_idempotency_conflict_delivers_nothing(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}
    client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    response = client.post(ENDPOINT, data={"email": "other@example.com"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert len(webhook.received) == 1
    assert len(attempts(client)) == 1


def test_distinct_submissions_each_deliver(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"email": "one@example.com"})
    client.post(ENDPOINT, data={"email": "two@example.com"})

    assert len(webhook.received) == 2
    assert len(attempts(client)) == 2


# --- rejected submissions never deliver --------------------------------------


@pytest.mark.parametrize(
    ("description", "kwargs"),
    [
        ("empty submission", {"data": {}}),
        ("unsupported content type", {"json": {"email": "a@b.co"}}),
        (
            "malformed multipart",
            {"content": b"--x\r\nnope", "headers": {"content-type": "multipart/form-data"}},
        ),
    ],
)
def test_a_rejected_submission_delivers_nothing(
    make_client: ClientFactory,
    webhook: WebhookRecorder,
    description: str,
    kwargs: dict[str, Any],
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, **kwargs)

    assert response.status_code >= 400, description
    assert webhook.received == []
    assert attempts(client) == []


def test_an_inactive_endpoint_delivers_nothing(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, "closed-form", is_active=False, webhook_url=webhook.url)

    response = client.post("/f/closed-form", data={"email": "dev@example.com"})

    assert response.status_code == 409
    assert webhook.received == []


# --- the secret stays put ----------------------------------------------------


def test_the_secret_never_reaches_the_destination(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = hooked_client(make_client, webhook.url)
    secret = endpoint["webhook_secret"]

    client.post(ENDPOINT, data={"email": "dev@example.com"})

    delivered = webhook.received[0]
    assert secret not in delivered.body.decode("utf-8")
    assert all(secret not in value for value in delivered.headers.values())


def test_the_secret_never_appears_in_a_submission_response(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert endpoint["webhook_secret"] not in response.text


def test_the_secret_never_appears_in_an_error(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 500
    client, endpoint = hooked_client(make_client, webhook.url)
    secret = endpoint["webhook_secret"]
    headers = {"Idempotency-Key": KEY}
    client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    conflict = client.post(ENDPOINT, data={"email": "other@example.com"}, headers=headers)
    not_found = client.post("/f/no-such-form", data={"email": "dev@example.com"})

    assert secret not in conflict.text
    assert secret not in not_found.text


def test_no_log_output_carries_the_secret(
    make_client: ClientFactory, webhook: WebhookRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    webhook.status = 500
    client, endpoint = hooked_client(make_client, webhook.url)

    with caplog.at_level(logging.DEBUG):
        client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert endpoint["webhook_secret"] not in caplog.text


def test_the_secret_is_never_stored_on_the_attempt(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = hooked_client(make_client, unused_local_url())

    client.post(ENDPOINT, data={"email": "dev@example.com"})

    attempt = attempts(client)[0]
    assert endpoint["webhook_secret"] not in (attempt.error or "")
    assert endpoint["webhook_secret"] not in attempt.destination_url
