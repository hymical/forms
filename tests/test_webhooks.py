"""
the ingestion side of webhooks: configuration, and queueing work without sending it

Delivery itself belongs to the worker and is covered in ``test_worker.py``. What
matters here is that accepting a submission creates the durable obligation and
makes no outbound request while doing it.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from conftest import ClientFactory, create_endpoint, open_session
from hymical_forms import models
from hymical_forms.webhooks import DeliveryState
from webhook_server import WebhookRecorder

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
    overrides.setdefault("allow_private_webhook_targets", True)
    client = make_client(seed_endpoint=False, **overrides)
    endpoint = create_endpoint(client, webhook_url=url)
    return client, endpoint


def deliveries(client: TestClient) -> list[models.WebhookDelivery]:
    """
    read every queued delivery behind a client
    :param client: the client whose application database should be inspected
    :returns: the delivery rows
    """
    with open_session(client) as session:
        return list(session.scalars(select(models.WebhookDelivery)))


def attempts(client: TestClient) -> list[models.DeliveryAttempt]:
    """
    read every recorded delivery attempt behind a client
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


# --- queueing, not sending ---------------------------------------------------


def test_accepting_a_submission_queues_a_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["delivery"] == {"queued": True}
    queued = deliveries(client)
    assert len(queued) == 1
    assert queued[0].submission_id == response.json()["submission_id"]
    assert queued[0].state == DeliveryState.PENDING
    assert queued[0].attempts == 0


def test_ingestion_makes_no_outbound_request(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    the form request must return without waiting on anybody else's server
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server that would record a delivery if one happened
    """
    client, _ = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert webhook.received == []
    assert attempts(client) == []


def test_ingestion_is_not_slowed_by_a_hanging_destination(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a destination that never answers cannot couple itself to ingestion latency
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server, set to stall well past any ingestion timeout
    """
    webhook.delay_seconds = 30.0
    client, _ = hooked_client(make_client, webhook.url)

    # No timeout is needed to make this fast, because nothing is dialled at all.
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert webhook.received == []


def test_the_queued_delivery_snapshots_the_destination_and_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a delivery owes what was configured when it was accepted, not what is configured later
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, endpoint = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"email": "dev@example.com"})

    queued = deliveries(client)[0]
    assert queued.destination_url == webhook.url
    assert queued.signing_secret == endpoint["webhook_secret"]


def test_the_first_attempt_is_due_immediately(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    queued = deliveries(client)[0]
    assert queued.next_attempt_at.isoformat() == response.json()["received_at"].replace(
        "Z", "+00:00"
    )
    assert queued.claim_expires_at is None
    assert queued.completed_at is None


def test_without_a_webhook_nothing_is_queued(client: TestClient) -> None:
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["delivery"] == {"queued": False}
    assert deliveries(client) == []


def test_each_submission_queues_its_own_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    client.post(ENDPOINT, data={"email": "one@example.com"})
    client.post(ENDPOINT, data={"email": "two@example.com"})

    assert len(deliveries(client)) == 2


# --- rejected submissions queue nothing --------------------------------------


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
def test_a_rejected_submission_queues_nothing(
    make_client: ClientFactory,
    webhook: WebhookRecorder,
    description: str,
    kwargs: dict[str, Any],
) -> None:
    client, _ = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, **kwargs)

    assert response.status_code >= 400, description
    assert deliveries(client) == []
    with open_session(client) as session:
        assert list(session.scalars(select(models.Submission))) == []


def test_an_inactive_endpoint_queues_nothing(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, "closed-form", is_active=False, webhook_url=webhook.url)

    response = client.post("/f/closed-form", data={"email": "dev@example.com"})

    assert response.status_code == 409
    assert deliveries(client) == []


def test_a_storage_failure_leaves_neither_a_submission_nor_delivery_work(
    make_client: ClientFactory, webhook: WebhookRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    the pair is one transaction, so a failure cannot leave half of it behind
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    :param monkeypatch: pytest fixture used to break the commit for one request
    """
    client, _ = hooked_client(make_client, webhook.url)

    def failing_commit(self: Session) -> None:
        raise OperationalError("INSERT INTO submissions", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "commit", failing_commit)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    monkeypatch.undo()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    with open_session(session_client := client) as session:
        assert list(session.scalars(select(models.Submission))) == []
        assert list(session.scalars(select(models.WebhookDelivery))) == []
    assert session_client is client


def test_concurrent_identical_submissions_queue_one_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder, tmp_path: Path
) -> None:
    """
    the losers of the idempotency race must not each add their own delivery
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    :param tmp_path: pytest-provided directory to hold a file-backed database
    """
    # A file-backed database, because the in-memory one is pinned to a single
    # connection and cannot express two writers racing.
    client = make_client(
        seed_endpoint=False,
        allow_private_webhook_targets=True,
        database_url=f"sqlite:///{tmp_path.as_posix()}/forms.db",
    )
    create_endpoint(client, webhook_url=webhook.url)
    headers = {"Idempotency-Key": KEY}

    barrier = threading.Barrier(6)

    def send() -> Any:
        barrier.wait()
        return client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = [future.result() for future in [pool.submit(send) for _ in range(6)]]

    assert [response.status_code for response in responses] == [202] * 6
    assert len({response.json()["submission_id"] for response in responses}) == 1
    with open_session(client) as session:
        assert len(list(session.scalars(select(models.Submission)))) == 1
        assert len(list(session.scalars(select(models.WebhookDelivery)))) == 1


# --- idempotency interaction -------------------------------------------------


def test_a_replay_queues_no_second_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a client retrying a lost response must not create a second downstream obligation
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}

    first = client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)
    second = client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    assert first.status_code == second.status_code == 202
    assert second.json()["idempotent_replay"] is True
    assert second.json()["delivery"] == {"queued": True}
    assert len(deliveries(client)) == 1


def test_many_replays_still_queue_one_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}

    for _ in range(4):
        client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    assert len(deliveries(client)) == 1


def test_a_replay_does_not_disturb_the_retry_schedule(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    replaying must not reset a delivery that a worker has already been working on
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}
    client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)
    before = deliveries(client)[0]
    original = (before.state, before.attempts, before.next_attempt_at)

    client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    after = deliveries(client)[0]
    assert (after.state, after.attempts, after.next_attempt_at) == original


def test_an_idempotency_conflict_queues_nothing_new(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = hooked_client(make_client, webhook.url)
    headers = {"Idempotency-Key": KEY}
    client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    response = client.post(ENDPOINT, data={"email": "other@example.com"}, headers=headers)

    assert response.status_code == 409
    assert len(deliveries(client)) == 1


# --- the secret stays put ----------------------------------------------------


def test_the_secret_never_appears_in_a_submission_response(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = hooked_client(make_client, webhook.url)

    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert endpoint["webhook_secret"] not in response.text


def test_the_secret_never_appears_in_an_error(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = hooked_client(make_client, webhook.url)
    secret = endpoint["webhook_secret"]
    headers = {"Idempotency-Key": KEY}
    client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    conflict = client.post(ENDPOINT, data={"email": "other@example.com"}, headers=headers)
    not_found = client.post("/f/no-such-form", data={"email": "dev@example.com"})

    assert secret not in conflict.text
    assert secret not in not_found.text


def test_no_ingestion_log_output_carries_the_secret(
    make_client: ClientFactory, webhook: WebhookRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    client, endpoint = hooked_client(make_client, webhook.url)

    with caplog.at_level(logging.DEBUG):
        client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert endpoint["webhook_secret"] not in caplog.text
