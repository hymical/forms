"""
manual replay: ``POST /deliveries/{delivery_id}/replay``

Replay is a state change and nothing else. Every test here proves that by
letting the ordinary worker do the sending afterwards, exactly as it would for
a delivery nobody touched.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import (
    ClientFactory,
    bearer,
    create_endpoint,
    management_key,
    open_session,
    work_once,
)
from hymical_forms import models, storage
from hymical_forms.webhooks import DeliveryState
from webhook_server import WebhookRecorder

REPLAY_PATH = "/deliveries/{delivery_id}/replay"


def failing_client(
    make_client: ClientFactory, webhook: WebhookRecorder, *, status: int = 400, **overrides: Any
) -> TestClient:
    """
    build a client whose endpoint delivers to a destination that refuses everything
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server standing in for the receiver
    :param status: the status that destination answers with, final by default
    :param overrides: extra setting overrides for the application
    :returns: a client with the default endpoint pointed at that destination
    """
    overrides.setdefault("allow_private_webhook_targets", True)
    webhook.status = status
    client = make_client(seed_endpoint=False, **overrides)
    create_endpoint(client, webhook_url=webhook.url)
    return client


def delivery_of(client: TestClient) -> models.WebhookDelivery:
    """
    read the single queued delivery behind a client
    :param client: the client whose application database should be inspected
    :returns: the one delivery row
    """
    with open_session(client) as session:
        return session.scalars(select(models.WebhookDelivery)).one()


def attempts_of(client: TestClient) -> list[models.DeliveryAttempt]:
    """
    read every recorded attempt behind a client, oldest first
    :param client: the client whose application database should be inspected
    :returns: the attempt rows ordered by attempt number
    """
    with open_session(client) as session:
        return list(
            session.scalars(
                select(models.DeliveryAttempt).order_by(models.DeliveryAttempt.attempt_number)
            )
        )


def fail_once(client: TestClient, webhook: WebhookRecorder) -> str:
    """
    submit a form and let the worker drive its delivery to terminal failure
    :param client: the client to submit through
    :param webhook: the local server refusing the delivery
    :returns: the identifier of the now-failed delivery
    """
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    work_once(client, now=delivery_of(client).next_attempt_at)
    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.FAILED, delivery.state
    return delivery.id


# --- authentication ----------------------------------------------------------


def test_replay_refuses_an_unauthenticated_request(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    client.headers.pop("Authorization")

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert delivery_of(client).state == DeliveryState.FAILED


def test_replay_refuses_an_unusable_key(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)

    response = client.post(
        REPLAY_PATH.format(delivery_id=delivery_id), headers=bearer("hym_live_nope")
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert delivery_of(client).state == DeliveryState.FAILED


# --- eligibility -------------------------------------------------------------


def test_an_unknown_delivery_is_a_404(client: TestClient) -> None:
    response = client.post(REPLAY_PATH.format(delivery_id="whd_nothing"))

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "delivery_not_found"
    assert body["error"]["details"]["delivery_id"] == "whd_nothing"


def test_a_pending_delivery_cannot_be_replayed(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    delivery_id = delivery_of(client).id

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "delivery_not_replayable"
    assert body["error"]["details"]["state"] == "pending"


def test_a_processing_delivery_cannot_be_replayed(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    delivery_id = delivery_of(client).id
    with open_session(client) as session:
        storage.claim_due_deliveries(
            session, now=datetime.now(UTC) + timedelta(hours=1), lease_seconds=3600, limit=1
        )

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 409
    assert response.json()["error"]["details"]["state"] == "processing"


def test_a_delivered_delivery_cannot_be_replayed(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    replay is a repair for failure, not a way to send a receiver the same event twice
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client = failing_client(make_client, webhook, status=200)
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    work_once(client)
    delivery_id = delivery_of(client).id

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 409
    assert response.json()["error"]["details"]["state"] == "delivered"
    assert len(webhook.received) == 1


def test_a_failed_delivery_can_be_replayed(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == delivery_id
    assert body["state"] == "pending"
    assert body["completed_at"] is None
    assert body["cycle_attempt_count"] == 0
    assert body["attempt_count"] == 1


def test_replaying_twice_is_refused_the_second_time(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    assert client.post(REPLAY_PATH.format(delivery_id=delivery_id)).status_code == 200

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 409
    assert response.json()["error"]["details"]["state"] == "pending"


# --- what replay must not do -------------------------------------------------


def test_replay_creates_no_new_submission(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    with open_session(client) as session:
        before = session.scalars(select(models.Submission)).one()

    client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    with open_session(client) as session:
        after = session.scalars(select(models.Submission)).one()
    assert after.id == before.id
    assert after.received_at == before.received_at
    assert after.fields == before.fields


def test_replay_creates_no_second_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)

    client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    with open_session(client) as session:
        deliveries = list(session.scalars(select(models.WebhookDelivery)))
    assert [delivery.id for delivery in deliveries] == [delivery_id]


def test_replay_keeps_the_historical_attempts(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(
        make_client,
        webhook,
        status=503,
        webhook_max_attempts=2,
        webhook_retry_initial_seconds=10,
    )
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    now = delivery_of(client).next_attempt_at
    work_once(client, now=now)
    work_once(client, now=delivery_of(client).next_attempt_at)
    before = [(attempt.id, attempt.attempt_number) for attempt in attempts_of(client)]
    assert len(before) == 2

    client.post(REPLAY_PATH.format(delivery_id=delivery_of(client).id))

    after = [(attempt.id, attempt.attempt_number) for attempt in attempts_of(client)]
    assert after == before


def test_replay_preserves_the_snapshotted_destination(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    before = delivery_of(client)
    destination, secret = before.destination_url, before.signing_secret

    body = client.post(REPLAY_PATH.format(delivery_id=delivery_id)).json()

    after = delivery_of(client)
    assert after.destination_url == destination
    assert after.signing_secret == secret
    assert body["destination_url"] == destination


def test_replay_never_returns_or_logs_the_signing_secret(
    make_client: ClientFactory, webhook: WebhookRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """
    the signing identity has to survive a replay without ever being shown
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server refusing the delivery
    :param caplog: pytest fixture capturing everything that was logged
    """
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    secret = delivery_of(client).signing_secret
    key = management_key(client)

    with caplog.at_level(logging.DEBUG):
        response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert secret not in response.text
    assert secret not in caplog.text
    assert key not in caplog.text
    assert key not in response.text


def test_replay_sends_nothing_itself(make_client: ClientFactory, webhook: WebhookRecorder) -> None:
    """
    the API process makes no outbound request, and replay must not be the exception
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server that would record a delivery if one were sent
    """
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    webhook.status = 200
    webhook.received.clear()

    response = client.post(REPLAY_PATH.format(delivery_id=delivery_id))

    assert response.status_code == 200
    assert webhook.received == [], "the API process sent the webhook itself"
    assert attempts_of(client)[-1].attempt_number == 1


# --- the worker picks it up --------------------------------------------------


def test_the_worker_claims_a_replayed_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    webhook.status = 200
    webhook.received.clear()

    client.post(REPLAY_PATH.format(delivery_id=delivery_id))
    handled = work_once(client)

    assert handled == 1
    assert len(webhook.received) == 1


def test_a_replayed_delivery_that_succeeds_becomes_delivered(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    webhook.status = 200

    client.post(REPLAY_PATH.format(delivery_id=delivery_id))
    work_once(client)

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.DELIVERED
    assert delivery.completed_at is not None
    assert delivery.attempts == 2
    assert delivery.cycle_attempts == 1


def test_a_replayed_attempt_continues_the_numbering(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    an attempt number must never be reused, so the history stays readable
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server refusing then accepting the delivery
    """
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    webhook.status = 200

    client.post(REPLAY_PATH.format(delivery_id=delivery_id))
    work_once(client)

    recorded = attempts_of(client)
    assert [attempt.attempt_number for attempt in recorded] == [1, 2]
    assert recorded[0].outcome == "http_error"
    assert recorded[1].outcome == "succeeded"
    assert len({attempt.id for attempt in recorded}) == 2


def test_a_replay_earns_a_whole_retry_cycle(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    the point of the cycle counter: a spent delivery must not fail on its first retry
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server answering with a retryable status throughout
    """
    client = failing_client(
        make_client,
        webhook,
        status=503,
        webhook_max_attempts=3,
        webhook_retry_initial_seconds=10,
    )
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    now = delivery_of(client).next_attempt_at
    for _ in range(3):
        work_once(client, now=now)
        now = delivery_of(client).next_attempt_at + timedelta(seconds=1)
    assert delivery_of(client).state == DeliveryState.FAILED
    assert delivery_of(client).attempts == 3

    client.post(REPLAY_PATH.format(delivery_id=delivery_of(client).id))
    replayed = delivery_of(client).next_attempt_at
    for _ in range(3):
        work_once(client, now=replayed)
        replayed = delivery_of(client).next_attempt_at + timedelta(seconds=1)

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.FAILED
    assert delivery.attempts == 6, "the replayed cycle did not get a full allowance"
    assert delivery.cycle_attempts == 3
    assert [attempt.attempt_number for attempt in attempts_of(client)] == [1, 2, 3, 4, 5, 6]


def test_the_backoff_starts_again_from_the_first_delay(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(
        make_client,
        webhook,
        status=503,
        webhook_max_attempts=2,
        webhook_retry_initial_seconds=10,
    )
    client.post("/f/contact-form", data={"email": "dev@example.com"})
    now = delivery_of(client).next_attempt_at
    work_once(client, now=now)
    work_once(client, now=delivery_of(client).next_attempt_at)
    client.post(REPLAY_PATH.format(delivery_id=delivery_of(client).id))

    replayed_at = delivery_of(client).next_attempt_at
    work_once(client, now=replayed_at)

    assert delivery_of(client).next_attempt_at == replayed_at + timedelta(seconds=10)


def test_the_replayed_delivery_carries_the_original_submission(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    client.post("/f/contact-form", data={"email": "dev@example.com", "note": "keep me"})
    work_once(client, now=delivery_of(client).next_attempt_at)
    delivery_id = delivery_of(client).id
    webhook.status = 200
    webhook.received.clear()

    client.post(REPLAY_PATH.format(delivery_id=delivery_id))
    work_once(client)

    payload = json.loads(webhook.received[0].body)["submission"]
    assert payload["id"] == delivery_of(client).submission_id
    assert payload["fields"] == {"email": ["dev@example.com"], "note": ["keep me"]}


def test_the_detail_route_shows_the_whole_history_after_a_replay(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = failing_client(make_client, webhook)
    delivery_id = fail_once(client, webhook)
    webhook.status = 200
    client.post(REPLAY_PATH.format(delivery_id=delivery_id))
    work_once(client)

    body = client.get(f"/deliveries/{delivery_id}").json()

    assert [attempt["attempt_number"] for attempt in body["attempts"]] == [1, 2]
    assert body["attempt_count"] == 2
    assert body["cycle_attempt_count"] == 1
    assert body["state"] == "delivered"
