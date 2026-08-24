"""
worker claiming against real PostgreSQL

This is the behaviour SQLite cannot model. SQLite silently ignores ``FOR UPDATE``
and serialises writers anyway, so the fast suite can only ever show that the
conditional update guard works. What matters in production is that two workers
holding two connections are handed different rows and neither waits on the
other, and that can only be shown here.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models, storage
from hymical_forms.webhooks import DeliveryState
from integration.support import seed_due_deliveries, seed_endpoint

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def locking_select(now: datetime, limit: int) -> Any:
    """
    build the same locking read the claim uses
    :param now: the instant to judge dueness against
    :param limit: the most rows to lock
    :returns: a select statement that locks and skips locked rows
    """
    return (
        select(models.WebhookDelivery)
        .where(storage.due_condition(now))
        .order_by(models.WebhookDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def test_a_locked_row_is_skipped_rather_than_waited_on(
    sessions: sessionmaker[Session],
) -> None:
    """
    the defining property of SKIP LOCKED, shown with two real open transactions
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
        seed_due_deliveries(setup, 2, now=NOW)

    with sessions() as first, sessions() as second:
        # First worker locks a row and deliberately does not commit, standing in
        # for a worker that is still mid-claim.
        locked_by_first = first.scalars(locking_select(NOW, 1)).all()
        assert len(locked_by_first) == 1

        started = time.monotonic()
        locked_by_second = second.scalars(locking_select(NOW, 1)).all()
        elapsed = time.monotonic() - started

        assert len(locked_by_second) == 1
        assert {row.id for row in locked_by_first}.isdisjoint({row.id for row in locked_by_second})
        assert elapsed < 5, "the second worker blocked on the first worker's lock"

        first.rollback()
        second.rollback()


def test_the_only_due_row_is_skipped_when_another_worker_holds_it(
    sessions: sessionmaker[Session],
) -> None:
    """
    with one row and two workers, the second must come away empty, not blocked
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
        seed_due_deliveries(setup, 1, now=NOW)

    with sessions() as first, sessions() as second:
        assert len(first.scalars(locking_select(NOW, 10)).all()) == 1

        started = time.monotonic()
        locked_by_second = second.scalars(locking_select(NOW, 10)).all()
        elapsed = time.monotonic() - started

        assert locked_by_second == []
        assert elapsed < 5, "the second worker blocked instead of skipping"

        first.rollback()
        second.rollback()


def test_concurrent_workers_never_claim_the_same_delivery(
    sessions: sessionmaker[Session],
) -> None:
    """
    six real sessions claiming at once must partition the work, never share it
    :param sessions: factory handing out independent connections
    """
    workers = 6
    total = 24
    with sessions() as setup:
        seed_endpoint(setup)
        expected = set(seed_due_deliveries(setup, total, now=NOW))

    barrier = threading.Barrier(workers)

    def claim() -> list[str]:
        barrier.wait()
        with sessions() as session:
            claimed = storage.claim_due_deliveries(session, now=NOW, lease_seconds=60, limit=total)
            return [job.id for job in claimed]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = [future.result() for future in [pool.submit(claim) for _ in range(workers)]]

    claimed = [delivery_id for batch in batches for delivery_id in batch]
    assert len(claimed) == len(set(claimed)), "a delivery was claimed by more than one worker"
    assert set(claimed) == expected, "some due deliveries were never claimed"

    # And the database agrees: everything is processing, held by somebody.
    with sessions() as session:
        rows = list(session.scalars(select(models.WebhookDelivery)))
    assert {row.state for row in rows} == {DeliveryState.PROCESSING}
    assert all(row.claim_expires_at == NOW + timedelta(seconds=60) for row in rows)


def test_concurrent_workers_share_the_work_out(sessions: sessionmaker[Session]) -> None:
    """
    SKIP LOCKED should let workers proceed in parallel, not funnel into one
    :param sessions: factory handing out independent connections
    """
    workers = 4
    with sessions() as setup:
        seed_endpoint(setup)
        seed_due_deliveries(setup, 40, now=NOW)

    barrier = threading.Barrier(workers)

    def claim() -> int:
        barrier.wait()
        with sessions() as session:
            return len(storage.claim_due_deliveries(session, now=NOW, lease_seconds=60, limit=5))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = [future.result() for future in [pool.submit(claim) for _ in range(workers)]]

    assert sum(counts) == workers * 5, "workers did not each get a full batch"
    assert all(count == 5 for count in counts)


def test_a_claim_survives_in_the_database(sessions: sessionmaker[Session]) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_due_deliveries(setup, 1, now=NOW)[0]

    with sessions() as worker:
        storage.claim_due_deliveries(worker, now=NOW, lease_seconds=60, limit=10)

    # A different connection entirely, so this is the committed state.
    with sessions() as observer:
        row = observer.get(models.WebhookDelivery, delivery_id)
    assert row is not None
    assert row.state == DeliveryState.PROCESSING
    assert row.claim_expires_at == NOW + timedelta(seconds=60)


def test_an_active_lease_is_not_reclaimable(sessions: sessionmaker[Session]) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        seed_due_deliveries(setup, 1, now=NOW)
    with sessions() as first:
        assert len(storage.claim_due_deliveries(first, now=NOW, lease_seconds=60, limit=10)) == 1

    with sessions() as second:
        again = storage.claim_due_deliveries(
            second, now=NOW + timedelta(seconds=30), lease_seconds=60, limit=10
        )

    assert again == []


def test_an_expired_lease_is_reclaimable(sessions: sessionmaker[Session]) -> None:
    """
    a worker that died holding a delivery must not strand it forever
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_due_deliveries(setup, 1, now=NOW)[0]
    with sessions() as abandoned:
        storage.claim_due_deliveries(abandoned, now=NOW, lease_seconds=60, limit=10)

    with sessions() as recovering:
        reclaimed = storage.claim_due_deliveries(
            recovering, now=NOW + timedelta(seconds=61), lease_seconds=60, limit=10
        )

    assert [job.id for job in reclaimed] == [delivery_id]


def test_two_workers_racing_to_reclaim_an_expired_lease_do_not_both_win(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        seed_due_deliveries(setup, 1, now=NOW)
    with sessions() as abandoned:
        storage.claim_due_deliveries(abandoned, now=NOW, lease_seconds=60, limit=10)

    later = NOW + timedelta(seconds=61)
    barrier = threading.Barrier(2)

    def reclaim() -> list[str]:
        barrier.wait()
        with sessions() as session:
            return [
                job.id
                for job in storage.claim_due_deliveries(
                    session, now=later, lease_seconds=60, limit=10
                )
            ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (future.result() for future in [pool.submit(reclaim) for _ in range(2)])

    assert len(first) + len(second) == 1, "both workers reclaimed the same expired delivery"


def test_a_terminal_delivery_is_never_claimed(sessions: sessionmaker[Session]) -> None:
    with sessions() as setup:
        seed_endpoint(setup)
        delivery_id = seed_due_deliveries(setup, 1, now=NOW)[0]
        delivery = setup.get(models.WebhookDelivery, delivery_id)
        assert delivery is not None
        delivery.state = DeliveryState.DELIVERED
        delivery.completed_at = NOW
        setup.commit()

    with sessions() as worker:
        claimed = storage.claim_due_deliveries(
            worker, now=NOW + timedelta(days=1), lease_seconds=60, limit=10
        )

    assert claimed == []
