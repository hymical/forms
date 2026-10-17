"""
the delivery worker: claiming, sending, retrying, and giving up

Time is supplied explicitly to every worker call, so retry schedules and lease
expiry are asserted exactly rather than waited for. Nothing here sleeps, and
nothing here leaves the loopback interface.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from conftest import (
    ClientFactory,
    app_settings,
    build_settings,
    create_endpoint,
    open_session,
    work_once,
)
from hymical_forms import models, storage, worker
from hymical_forms.db import create_engine_from_url
from hymical_forms.delivery import create_webhook_client, deliver
from hymical_forms.schema import SchemaNotReady, create_all
from hymical_forms.webhooks import DeliveryResult, DeliveryState, RetryPolicy
from webhook_server import WebhookRecorder, unused_local_url

ENDPOINT = "/f/contact-form"


def queued_client(
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


def delivery_of(client: TestClient) -> models.WebhookDelivery:
    """
    read the single queued delivery behind a client
    :param client: the client whose application database should be inspected
    :returns: the one delivery row
    """
    with open_session(client) as session:
        return session.scalars(select(models.WebhookDelivery)).one()


def due_at(client: TestClient) -> datetime:
    """
    read the instant the queued delivery first becomes due
    :param client: the client whose application database should be inspected
    :returns: the delivery's next attempt time
    """
    # Anchoring to this rather than to a fixed constant keeps every schedule
    # assertion exact without any test needing to know real wall-clock time.
    return delivery_of(client).next_attempt_at


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


# --- successful delivery -----------------------------------------------------


def test_the_worker_delivers_a_queued_submission(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    handled = work_once(client, now=now)

    assert handled == 1
    assert len(webhook.received) == 1
    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.DELIVERED
    assert delivery.attempts == 1
    assert delivery.completed_at == now
    assert delivery.claim_expires_at is None


def test_the_delivered_payload_matches_the_contract(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    response = client.post(
        ENDPOINT, data={"email": "dev@example.com", "topics": ["billing", "api"]}
    )

    now = due_at(client)
    work_once(client, now=now)

    payload = json.loads(webhook.received[0].body)
    assert payload == {
        "type": "submission.received",
        "submission": {
            "id": response.json()["submission_id"],
            "endpoint_id": "contact-form",
            "received_at": response.json()["received_at"],
            "fields": {"email": ["dev@example.com"], "topics": ["billing", "api"]},
        },
    }


def test_the_signature_still_verifies_against_the_exact_bytes(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    moving delivery into the worker must not change what a receiver has to do
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, endpoint = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com", "note": "héllo, wörld"})

    now = due_at(client)
    work_once(client, now=now)

    delivered = webhook.received[0]
    expected = hmac.new(
        endpoint["webhook_secret"].encode("utf-8"), delivered.body, hashlib.sha256
    ).hexdigest()
    assert delivered.headers["hymical-signature"] == f"v1={expected}"


def test_a_successful_attempt_is_recorded(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    recorded = attempts_of(client)
    assert len(recorded) == 1
    attempt = recorded[0]
    assert attempt.delivery_id == delivery_of(client).id
    assert attempt.submission_id == response.json()["submission_id"]
    assert attempt.attempt_number == 1
    assert attempt.attempted_at == now
    assert attempt.outcome == "succeeded"
    assert attempt.response_status == 200
    assert attempt.error is None


def test_a_delivered_job_is_not_picked_up_again(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    now = due_at(client)
    work_once(client, now=now)

    handled = work_once(client, now=now + timedelta(hours=1))

    assert handled == 0
    assert len(webhook.received) == 1


def test_the_worker_has_nothing_to_do_without_a_webhook(client: TestClient) -> None:
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert work_once(client) == 0
    assert attempts_of(client) == []


def test_the_worker_handles_a_whole_batch(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    for index in range(3):
        client.post(ENDPOINT, data={"email": f"dev{index}@example.com"})

    handled = work_once(client)

    assert handled == 3
    assert len(webhook.received) == 3
    with open_session(client) as session:
        states = {row.state for row in session.scalars(select(models.WebhookDelivery))}
    assert states == {DeliveryState.DELIVERED}


# --- retry -------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 408, 425, 429])
def test_a_retryable_response_schedules_another_attempt(
    make_client: ClientFactory, webhook: WebhookRecorder, status: int
) -> None:
    webhook.status = status
    client, _ = queued_client(make_client, webhook.url, webhook_retry_initial_seconds=10)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.PENDING
    assert delivery.attempts == 1
    assert delivery.next_attempt_at == now + timedelta(seconds=10)
    assert delivery.completed_at is None


def test_a_timeout_schedules_another_attempt(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.delay_seconds = 2.0
    client, _ = queued_client(
        make_client,
        webhook.url,
        webhook_read_timeout_seconds=0.25,
        webhook_retry_initial_seconds=10,
    )
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.PENDING
    assert delivery.next_attempt_at == now + timedelta(seconds=10)
    assert attempts_of(client)[0].outcome == "timeout"


def test_a_refused_connection_schedules_another_attempt(make_client: ClientFactory) -> None:
    client, _ = queued_client(make_client, unused_local_url(), webhook_retry_initial_seconds=10)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.PENDING
    assert delivery.next_attempt_at == now + timedelta(seconds=10)
    assert attempts_of(client)[0].outcome == "network_error"


def test_a_retry_is_not_made_before_it_is_due(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 503
    client, _ = queued_client(make_client, webhook.url, webhook_retry_initial_seconds=10)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    now = due_at(client)
    work_once(client, now=now)

    early = work_once(client, now=now + timedelta(seconds=9))

    assert early == 0
    assert len(webhook.received) == 1
    assert delivery_of(client).attempts == 1


def test_a_retry_is_made_once_due(make_client: ClientFactory, webhook: WebhookRecorder) -> None:
    webhook.status = 503
    client, _ = queued_client(make_client, webhook.url, webhook_retry_initial_seconds=10)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    now = due_at(client)
    work_once(client, now=now)

    handled = work_once(client, now=now + timedelta(seconds=10))

    assert handled == 1
    assert len(webhook.received) == 2
    assert delivery_of(client).attempts == 2


def test_the_backoff_doubles_and_then_caps(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    every wait in the schedule is asserted, not just the first
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server, answering with a retryable status throughout
    """
    webhook.status = 503
    client, _ = queued_client(
        make_client,
        webhook.url,
        webhook_max_attempts=6,
        webhook_retry_initial_seconds=10,
        webhook_retry_max_seconds=60,
    )
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    schedule = []
    now = due_at(client)
    for _ in range(5):
        work_once(client, now=now)
        delivery = delivery_of(client)
        gap = delivery.next_attempt_at - now
        schedule.append(int(gap.total_seconds()))
        now = delivery.next_attempt_at

    # 10, 20, 40, then held at the 60 second cap.
    assert schedule == [10, 20, 40, 60, 60]


def test_each_attempt_keeps_its_own_record(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 503
    client, _ = queued_client(make_client, webhook.url, webhook_retry_initial_seconds=10)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    for _ in range(3):
        work_once(client, now=now)
        now = delivery_of(client).next_attempt_at

    recorded = attempts_of(client)
    assert [attempt.attempt_number for attempt in recorded] == [1, 2, 3]
    assert all(attempt.outcome == "http_error" for attempt in recorded)
    assert len({attempt.id for attempt in recorded}) == 3
    assert delivery_of(client).attempts == 3


# --- terminal failure --------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422, 301, 302])
def test_a_non_retryable_response_is_final(
    make_client: ClientFactory, webhook: WebhookRecorder, status: int
) -> None:
    """
    repeating a request the receiver called malformed or already-seen will not repair it
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    :param status: the status the destination answers with
    """
    webhook.status = status
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.FAILED
    assert delivery.completed_at == now
    assert delivery.attempts == 1
    assert work_once(client, now=now + timedelta(days=1)) == 0


def test_running_out_of_attempts_is_final(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 503
    client, _ = queued_client(
        make_client, webhook.url, webhook_max_attempts=3, webhook_retry_initial_seconds=10
    )
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    for _ in range(3):
        work_once(client, now=now)
        now = delivery_of(client).next_attempt_at

    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.FAILED
    assert delivery.attempts == 3
    assert delivery.completed_at is not None
    assert len(webhook.received) == 3
    assert work_once(client, now=now + timedelta(days=1)) == 0


def test_the_final_attempt_stays_in_the_history(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 503
    client, _ = queued_client(
        make_client, webhook.url, webhook_max_attempts=2, webhook_retry_initial_seconds=10
    )
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)
    work_once(client, now=delivery_of(client).next_attempt_at)

    recorded = attempts_of(client)
    assert [attempt.attempt_number for attempt in recorded] == [1, 2]
    assert recorded[-1].response_status == 503
    assert delivery_of(client).state == DeliveryState.FAILED


# --- claiming and lease recovery ---------------------------------------------


def test_claiming_marks_the_job_as_processing(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url, worker_lease_seconds=60)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    with open_session(client) as session:
        claimed = storage.claim_due_deliveries(session, now=now, lease_seconds=60, limit=10)

    assert len(claimed) == 1
    delivery = delivery_of(client)
    assert delivery.state == DeliveryState.PROCESSING
    assert delivery.claim_expires_at == now + timedelta(seconds=60)


def test_a_claimed_job_is_not_claimable_by_another_worker(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    the second worker's guarded update matches nothing, so it walks away empty
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    with open_session(client) as first:
        claimed_by_first = storage.claim_due_deliveries(first, now=now, lease_seconds=60, limit=10)
    with open_session(client) as second:
        claimed_by_second = storage.claim_due_deliveries(
            second, now=now, lease_seconds=60, limit=10
        )

    assert len(claimed_by_first) == 1
    assert claimed_by_second == []


def test_a_claimed_job_is_skipped_by_a_later_batch(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url, worker_lease_seconds=60)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    now = due_at(client)
    with open_session(client) as session:
        storage.claim_due_deliveries(session, now=now, lease_seconds=60, limit=10)

    handled = work_once(client, now=now + timedelta(seconds=30))

    assert handled == 0
    assert webhook.received == []


def test_an_expired_lease_becomes_claimable_again(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    a worker that died holding a job must not leave it stuck in processing forever
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    client, _ = queued_client(make_client, webhook.url, worker_lease_seconds=60)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    # Claim it and then abandon it, exactly as a worker that was killed would.
    now = due_at(client)
    with open_session(client) as session:
        storage.claim_due_deliveries(session, now=now, lease_seconds=60, limit=10)

    handled = work_once(client, now=now + timedelta(seconds=61))

    assert handled == 1
    assert len(webhook.received) == 1
    assert delivery_of(client).state == DeliveryState.DELIVERED


def test_reclaiming_after_a_crash_can_deliver_twice(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    at-least-once is the honest guarantee, and this is what it costs
    :param make_client: factory for clients bound to a configured app
    :param webhook: the local server recording deliveries
    """
    # A worker that sends successfully and dies before recording it leaves a lease
    # that expires, and the next worker sends the same event again. Nothing in a
    # queue can prevent this without the receiver's cooperation.
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    now = due_at(client)
    with open_session(client) as session:
        claimed = storage.claim_due_deliveries(session, now=now, lease_seconds=60, limit=10)
    assert len(claimed) == 1

    work_once(client, now=now + timedelta(seconds=61))

    assert len(webhook.received) == 1
    delivered = json.loads(webhook.received[0].body)["submission"]["id"]
    assert delivered == delivery_of(client).submission_id


def test_a_batch_is_bounded_by_the_configured_size(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url, worker_batch_size=2)
    for index in range(5):
        client.post(ENDPOINT, data={"email": f"dev{index}@example.com"})

    handled = work_once(client)

    assert handled == 2
    assert len(webhook.received) == 2


def test_due_deliveries_are_taken_oldest_first(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url, worker_batch_size=1)
    first = client.post(ENDPOINT, data={"email": "one@example.com"}).json()
    client.post(ENDPOINT, data={"email": "two@example.com"})

    work_once(client)

    assert json.loads(webhook.received[0].body)["submission"]["id"] == first["submission_id"]


# --- the secret stays put ----------------------------------------------------


def test_the_secret_never_reaches_the_destination(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = queued_client(make_client, webhook.url)
    secret = endpoint["webhook_secret"]
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    delivered = webhook.received[0]
    assert secret not in delivered.body.decode("utf-8")
    assert all(secret not in value for value in delivered.headers.values())


def test_the_secret_is_never_stored_on_an_attempt(make_client: ClientFactory) -> None:
    client, endpoint = queued_client(make_client, unused_local_url())
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    attempt = attempts_of(client)[0]
    assert endpoint["webhook_secret"] not in (attempt.error or "")
    assert endpoint["webhook_secret"] not in attempt.destination_url


def test_a_stored_failure_message_is_bounded(make_client: ClientFactory) -> None:
    client, _ = queued_client(make_client, unused_local_url())
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    now = due_at(client)
    work_once(client, now=now)

    error = attempts_of(client)[0].error
    assert error is not None
    assert len(error) <= 500


# --- the retry policy itself -------------------------------------------------


def test_the_policy_doubles_from_the_initial_delay() -> None:
    policy = RetryPolicy(max_attempts=5, initial_seconds=10, max_seconds=3600)

    delays = [int(policy.delay_after(n).total_seconds()) for n in range(1, 6)]

    assert delays == [10, 20, 40, 80, 160]


def test_the_policy_respects_its_cap() -> None:
    policy = RetryPolicy(max_attempts=20, initial_seconds=10, max_seconds=100)

    assert int(policy.delay_after(10).total_seconds()) == 100


def test_the_policy_knows_when_the_allowance_is_gone() -> None:
    policy = RetryPolicy(max_attempts=3, initial_seconds=10, max_seconds=3600)

    assert not policy.is_exhausted(2)
    assert policy.is_exhausted(3)
    assert policy.is_exhausted(4)


def test_postgresql_claims_with_skip_locked() -> None:
    """
    the claim relies on row locking that only PostgreSQL provides, so pin the SQL
    """
    # SQLite silently drops FOR UPDATE, which is why the conditional update in
    # claim_due_deliveries exists as well. This asserts the PostgreSQL half,
    # which the SQLite-backed suite can never exercise at runtime.
    statement = (
        select(models.WebhookDelivery)
        .where(storage.due_condition(datetime.now(UTC)))
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    # SQLAlchemy does not type its dialect factories.
    compiled = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

    assert "FOR UPDATE SKIP LOCKED" in compiled


def test_the_configured_policy_reaches_the_worker(make_client: ClientFactory) -> None:
    client = make_client(webhook_max_attempts=7, webhook_retry_initial_seconds=3)

    policy = app_settings(client).retry_policy()

    assert policy.max_attempts == 7
    assert policy.initial_seconds == 3


# --- bounding one attempt ----------------------------------------------------


def test_a_trickling_response_cannot_outlast_the_attempt_deadline(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    # Each byte arrives well inside the read timeout, so only an overall deadline
    # can stop this. Without one the attempt runs for as long as the destination
    # cares to keep writing, which outlives any lease and stalls the whole batch.
    webhook.trickle_seconds = 0.05
    client, _ = queued_client(
        make_client,
        webhook.url,
        webhook_connect_timeout_seconds=0.25,
        webhook_read_timeout_seconds=0.25,
    )
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    started = time.monotonic()
    work_once(client, now=due_at(client))
    elapsed = time.monotonic() - started

    assert elapsed < 3
    attempt = attempts_of(client)[0]
    assert attempt.outcome == "timeout"
    assert delivery_of(client).state == DeliveryState.PENDING


def test_a_trickling_response_body_does_not_hold_up_a_success(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    # The status is the whole answer. Reading a body nobody looks at would let a
    # destination decide how long, and how much memory, an attempt takes.
    webhook.trickle_seconds = 0.05
    webhook.trickle_body = True
    client, _ = queued_client(
        make_client,
        webhook.url,
        webhook_connect_timeout_seconds=0.25,
        webhook_read_timeout_seconds=0.25,
    )
    client.post(ENDPOINT, data={"email": "dev@example.com"})

    started = time.monotonic()
    work_once(client, now=due_at(client))
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert attempts_of(client)[0].outcome == "succeeded"
    assert delivery_of(client).state == DeliveryState.DELIVERED


# --- the batch and its transaction -------------------------------------------


def test_no_transaction_is_open_while_requests_are_in_flight(
    make_client: ClientFactory, webhook: WebhookRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "dev@example.com"})
    settings = app_settings(client)
    observed: list[bool] = []

    async def run() -> None:
        webhook_client = create_webhook_client(settings)
        try:
            with open_session(client) as session:

                async def watching(*args: Any, **kwargs: Any) -> DeliveryResult:
                    observed.append(session.in_transaction())
                    return await deliver(*args, **kwargs)

                monkeypatch.setattr(worker, "deliver", watching)
                await worker.process_batch(session, webhook_client, settings, now=due_at(client))
        finally:
            await webhook_client.aclose()

    asyncio.run(run())

    assert observed == [False]
    assert delivery_of(client).state == DeliveryState.DELIVERED


def test_one_failed_recording_does_not_lose_the_rest_of_the_batch(
    make_client: ClientFactory, webhook: WebhookRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every request in the batch has already been sent by the time recording
    # starts. Abandoning the rest on the first database error would leave them
    # claimed with nothing recorded, to be sent a second time once the lease runs
    # out, for no reason other than a neighbour's failure.
    client, _ = queued_client(make_client, webhook.url)
    client.post(ENDPOINT, data={"email": "first@example.com"})
    client.post(ENDPOINT, data={"email": "second@example.com"})
    real = storage.complete_attempt
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> storage.CompletedAttempt:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("UPDATE", {}, Exception("connection reset"))
        return real(*args, **kwargs)

    monkeypatch.setattr(storage, "complete_attempt", flaky)
    with open_session(client) as session:
        latest = max(row.next_attempt_at for row in session.scalars(select(models.WebhookDelivery)))

    handled = work_once(client, now=latest)

    assert handled == 2
    assert len(webhook.received) == 2
    with open_session(client) as session:
        states = sorted(row.state for row in session.scalars(select(models.WebhookDelivery)))
    assert states == [DeliveryState.DELIVERED, DeliveryState.PROCESSING]


# --- starting up -------------------------------------------------------------


def test_the_worker_refuses_to_start_against_an_unmigrated_database(tmp_path: Path) -> None:
    settings = build_settings(database_url=f"sqlite:///{tmp_path / 'empty.db'}")
    stop = threading.Event()
    stop.set()

    with pytest.raises(SchemaNotReady):
        asyncio.run(worker.run_worker(settings, stop=stop))


def test_the_worker_warns_when_the_lease_cannot_outlast_an_attempt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"sqlite:///{tmp_path / 'forms.db'}"
    engine = create_engine_from_url(url)
    create_all(engine)
    engine.dispose()
    settings = build_settings(
        database_url=url,
        worker_lease_seconds=10,
        webhook_connect_timeout_seconds=5,
        webhook_read_timeout_seconds=10,
    )
    stop = threading.Event()
    stop.set()

    with caplog.at_level(logging.WARNING, logger="hymical_forms.worker"):
        asyncio.run(worker.run_worker(settings, stop=stop))

    assert "FORMS_WORKER_LEASE_SECONDS" in caplog.text


def test_the_default_lease_draws_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"sqlite:///{tmp_path / 'forms.db'}"
    engine = create_engine_from_url(url)
    create_all(engine)
    engine.dispose()
    stop = threading.Event()
    stop.set()

    with caplog.at_level(logging.WARNING, logger="hymical_forms.worker"):
        asyncio.run(worker.run_worker(build_settings(database_url=url), stop=stop))

    assert "FORMS_WORKER_LEASE_SECONDS" not in caplog.text
