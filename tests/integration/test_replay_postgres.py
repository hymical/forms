"""
manual replay against real PostgreSQL

Replay is a conditional state transition, so what matters is what two operators
pressing the button at the same moment get. SQLite serialises writers and would
make that race look safe whether or not it is, which is why it is settled here,
with independent connections and a real row lock.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models, storage
from hymical_forms.webhooks import DeliveryState
from integration.support import seed_endpoint, seed_failed_delivery

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def test_a_failed_delivery_is_requeued_once(sessions: sessionmaker[Session]) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)

    with sessions() as operator:
        outcome = storage.requeue_failed_delivery(operator, delivery_id, now=LATER)

    assert outcome.requeued is True
    with sessions() as observer:
        delivery = observer.get(models.WebhookDelivery, delivery_id)
    assert delivery is not None
    assert delivery.state == DeliveryState.PENDING
    assert delivery.completed_at is None
    assert delivery.cycle_attempts == 0
    assert delivery.attempts == 5
    assert delivery.next_attempt_at == LATER


def test_two_operators_replaying_at_once_do_not_both_win(
    sessions: sessionmaker[Session],
) -> None:
    """
    the losing request must be refused by the database, not by a check in Python
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)

    barrier = threading.Barrier(2)

    def replay() -> storage.ReplayOutcome:
        barrier.wait()
        with sessions() as session:
            return storage.requeue_failed_delivery(session, delivery_id, now=LATER)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (future.result() for future in [pool.submit(replay) for _ in range(2)])

    assert [first.requeued, second.requeued].count(True) == 1, "both replays claimed to win"
    # And the loser describes the state the database actually settled on, rather
    # than the one it hoped for, so its answer is the same however the race went.
    loser = first if not first.requeued else second
    assert loser.record is not None
    assert loser.record.delivery.state == DeliveryState.PENDING


def test_a_concurrent_replay_duplicates_no_work(sessions: sessionmaker[Session]) -> None:
    """
    six simultaneous replays must leave one delivery with one fresh retry cycle
    :param sessions: factory handing out independent connections
    """
    operators = 6
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)

    barrier = threading.Barrier(operators)

    def replay() -> bool:
        barrier.wait()
        with sessions() as session:
            return storage.requeue_failed_delivery(session, delivery_id, now=LATER).requeued

    with ThreadPoolExecutor(max_workers=operators) as pool:
        results = [future.result() for future in [pool.submit(replay) for _ in range(operators)]]

    assert results.count(True) == 1
    with sessions() as observer:
        assert observer.scalar(select(func.count()).select_from(models.WebhookDelivery)) == 1
        assert observer.scalar(select(func.count()).select_from(models.Submission)) == 1
        # The history is untouched: a replay resumes a delivery, it does not
        # rewrite what already happened to it.
        assert observer.scalar(select(func.count()).select_from(models.DeliveryAttempt)) == 5


def test_a_replayed_delivery_is_claimable_by_the_worker(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)
    with sessions() as operator:
        storage.requeue_failed_delivery(operator, delivery_id, now=LATER)

    with sessions() as worker:
        claimed = storage.claim_due_deliveries(worker, now=LATER, lease_seconds=60, limit=10)

    assert [job.id for job in claimed] == [delivery_id]


def test_a_delivery_that_was_never_failed_is_refused(sessions: sessionmaker[Session]) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)
        delivery = setup.get(models.WebhookDelivery, delivery_id)
        assert delivery is not None
        delivery.state = DeliveryState.DELIVERED
        setup.commit()

    with sessions() as operator:
        outcome = storage.requeue_failed_delivery(operator, delivery_id, now=LATER)

    assert outcome.requeued is False
    assert outcome.record is not None
    assert outcome.record.delivery.state == DeliveryState.DELIVERED


def test_replay_over_http_answers_from_the_settled_state(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    """
    the whole authenticated path, on the database this service is meant to run on
    :param pg_client: an API client backed by PostgreSQL
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)

    first = pg_client.post(f"/deliveries/{delivery_id}/replay")
    second = pg_client.post(f"/deliveries/{delivery_id}/replay")

    assert first.status_code == 200
    assert first.json()["state"] == "pending"
    assert first.json()["cycle_attempt_count"] == 0
    assert first.json()["attempt_count"] == 5
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "delivery_not_replayable"


def test_a_delivery_read_over_http_never_carries_the_snapshotted_secret(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_failed_delivery(setup, now=NOW)

    listing = pg_client.get("/deliveries")
    detail = pg_client.get(f"/deliveries/{delivery_id}")

    assert listing.status_code == 200
    assert detail.status_code == 200
    for response in (listing, detail):
        assert "whsec_" not in response.text
    assert [attempt["attempt_number"] for attempt in detail.json()["attempts"]] == [1, 2, 3, 4, 5]
