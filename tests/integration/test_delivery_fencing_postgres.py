"""
completion ownership against real PostgreSQL

A worker claims a delivery, spends as long as somebody else's server takes over
the request, and only then records what happened. Between those two moments its
lease can run out and another worker can legitimately take the delivery over.
What must not happen is the first worker coming back and overwriting the second
one's state.

That is a race between two connections holding two different claims on one row,
so SQLite cannot show it: it serialises writers and would make the sequence look
safe whether or not the fence exists. These tests hold the claims explicitly, in
independent sessions, and assert on what the database settled on.

Duplicate delivery is not what is under test here. Both workers may genuinely
have sent the request, and Hymical Forms still promises only at-least-once.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models, storage
from hymical_forms.webhooks import DeliveryOutcome, DeliveryResult, DeliveryState, RetryPolicy
from integration.support import seed_due_deliveries, seed_endpoint

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
LEASE = 60.0

# Long enough that a retryable failure schedules another attempt rather than
# exhausting the allowance, so the retry branch is the one being exercised.
POLICY = RetryPolicy(max_attempts=5, initial_seconds=10, max_seconds=3600)

SUCCESS = DeliveryResult(DeliveryOutcome.SUCCEEDED, response_status=200)
PERMANENT_FAILURE = DeliveryResult(
    DeliveryOutcome.HTTP_ERROR, response_status=404, error="destination responded with HTTP 404"
)
RETRYABLE_FAILURE = DeliveryResult(
    DeliveryOutcome.HTTP_ERROR, response_status=503, error="destination responded with HTTP 503"
)


def claim(session: Session, *, now: datetime) -> models.WebhookDelivery:
    """
    take the single due delivery as one worker, through the ordinary claim
    :param session: the session standing in for one worker's connection
    :param now: the instant that worker judges dueness against
    :returns: the claimed delivery, carrying the token it was claimed under
    """
    claimed = storage.claim_due_deliveries(session, now=now, lease_seconds=LEASE, limit=10)
    assert len(claimed) == 1, "the fixture should offer exactly one due delivery"
    return claimed[0]


def read(sessions: sessionmaker[Session], delivery_id: str) -> models.WebhookDelivery:
    """
    read a delivery back on a connection of its own, so this is the committed state
    :param sessions: factory handing out independent connections
    :param delivery_id: the delivery to read
    :returns: the delivery as the database now holds it
    """
    with sessions() as observer:
        delivery = observer.get(models.WebhookDelivery, delivery_id)
    assert delivery is not None
    return delivery


def attempt_numbers(sessions: sessionmaker[Session], delivery_id: str) -> list[int]:
    """
    read the attempt numbers recorded for a delivery, lowest first
    :param sessions: factory handing out independent connections
    :param delivery_id: the delivery whose history to read
    :returns: the recorded attempt numbers
    """
    with sessions() as observer:
        return [row.attempt_number for row in storage.list_delivery_attempts(observer, delivery_id)]


def seed_one(sessions: sessionmaker[Session]) -> str:
    """
    put one due delivery in the queue
    :param sessions: factory handing out independent connections
    :returns: the delivery id
    """
    with sessions() as setup:
        seed_endpoint(setup)
        return seed_due_deliveries(setup, 1, now=NOW)[0]


def test_a_superseded_worker_cannot_overwrite_the_current_owner(
    sessions: sessionmaker[Session],
) -> None:
    """
    the whole invariant in one sequence: A claims, A's lease lapses, B reclaims, A returns
    :param sessions: factory handing out independent connections
    """
    delivery_id = seed_one(sessions)
    reclaimed_at = NOW + timedelta(seconds=LEASE + 1)

    with sessions() as worker_a, sessions() as worker_b:
        job_a = claim(worker_a, now=NOW)
        # A's lease has lapsed by this point, so B is entitled to the delivery.
        job_b = claim(worker_b, now=reclaimed_at)
        assert job_b.claim_token != job_a.claim_token

        # A's request finally comes back and A tries to record it. Under the
        # unfenced implementation this set the row to delivered, cleared B's lease
        # and rewound the counters while B was still sending.
        stale = storage.complete_attempt(
            worker_a, job_a, SUCCESS, now=NOW, policy=POLICY, claim_token=job_a.claim_token
        )
        assert stale.owned is False

        held = read(sessions, delivery_id)
        assert held.state == DeliveryState.PROCESSING, "a superseded worker ended B's delivery"
        assert held.claim_token == job_b.claim_token, "a superseded worker took the claim back"
        assert held.claim_expires_at == reclaimed_at + timedelta(seconds=LEASE), (
            "a superseded worker cleared the current owner's lease"
        )
        assert held.completed_at is None
        assert held.cycle_attempts == 0, "a superseded worker spent the current retry cycle"

        # B, which still owns the claim, has the last word.
        owner = storage.complete_attempt(
            worker_b,
            job_b,
            PERMANENT_FAILURE,
            now=reclaimed_at,
            policy=POLICY,
            claim_token=job_b.claim_token,
        )
        assert owner.owned is True

    settled = read(sessions, delivery_id)
    assert settled.state == DeliveryState.FAILED
    assert settled.completed_at == reclaimed_at
    assert settled.claim_token is None
    assert settled.claim_expires_at is None


def test_fencing_follows_ownership_rather_than_the_result(
    sessions: sessionmaker[Session],
) -> None:
    """
    the opposing case: the superseded worker failed and the current owner succeeded
    :param sessions: factory handing out independent connections
    """
    # The mirror of the test above. If the fence were accidentally keyed on the
    # kind of result rather than on who holds the claim, one of these two
    # orderings would pass and the other would not.
    delivery_id = seed_one(sessions)
    reclaimed_at = NOW + timedelta(seconds=LEASE + 1)

    with sessions() as worker_a, sessions() as worker_b:
        job_a = claim(worker_a, now=NOW)
        job_b = claim(worker_b, now=reclaimed_at)

        storage.complete_attempt(
            worker_a,
            job_a,
            PERMANENT_FAILURE,
            now=NOW,
            policy=POLICY,
            claim_token=job_a.claim_token,
        )
        assert read(sessions, delivery_id).state == DeliveryState.PROCESSING

        storage.complete_attempt(
            worker_b,
            job_b,
            SUCCESS,
            now=reclaimed_at,
            policy=POLICY,
            claim_token=job_b.claim_token,
        )

    settled = read(sessions, delivery_id)
    assert settled.state == DeliveryState.DELIVERED, "a superseded failure buried a real success"
    assert settled.completed_at == reclaimed_at


def test_a_superseded_worker_does_not_move_the_retry_schedule(
    sessions: sessionmaker[Session],
) -> None:
    """
    the retry cycle and the next due time belong to whoever holds the claim
    :param sessions: factory handing out independent connections
    """
    delivery_id = seed_one(sessions)
    reclaimed_at = NOW + timedelta(seconds=LEASE + 1)

    with sessions() as worker_a, sessions() as worker_b:
        job_a = claim(worker_a, now=NOW)
        job_b = claim(worker_b, now=reclaimed_at)

        storage.complete_attempt(
            worker_a,
            job_a,
            RETRYABLE_FAILURE,
            now=NOW,
            policy=POLICY,
            claim_token=job_a.claim_token,
        )
        storage.complete_attempt(
            worker_b,
            job_b,
            RETRYABLE_FAILURE,
            now=reclaimed_at,
            policy=POLICY,
            claim_token=job_b.claim_token,
        )

    settled = read(sessions, delivery_id)
    assert settled.state == DeliveryState.PENDING
    # One cycle attempt, not two, so the first delay of the schedule is what the
    # owner earned rather than the second.
    assert settled.cycle_attempts == 1
    assert settled.next_attempt_at == reclaimed_at + timedelta(seconds=POLICY.initial_seconds)
    # Both requests really went out, and the lifetime count says so.
    assert settled.attempts == 2


def test_a_superseded_workers_request_stays_in_the_history(
    sessions: sessionmaker[Session],
) -> None:
    """
    a request that was really sent is really recorded, under a number of its own
    :param sessions: factory handing out independent connections
    """
    delivery_id = seed_one(sessions)
    reclaimed_at = NOW + timedelta(seconds=LEASE + 1)

    with sessions() as worker_a, sessions() as worker_b:
        job_a = claim(worker_a, now=NOW)
        job_b = claim(worker_b, now=reclaimed_at)

        stale = storage.complete_attempt(
            worker_a, job_a, SUCCESS, now=NOW, policy=POLICY, claim_token=job_a.claim_token
        )
        owner = storage.complete_attempt(
            worker_b,
            job_b,
            SUCCESS,
            now=reclaimed_at,
            policy=POLICY,
            claim_token=job_b.claim_token,
        )

    assert stale.attempt.attempt_number == 1
    assert owner.attempt.attempt_number == 2
    assert attempt_numbers(sessions, delivery_id) == [1, 2]
    assert read(sessions, delivery_id).attempts == 2


def test_two_workers_completing_at_once_never_reuse_an_attempt_number(
    sessions: sessionmaker[Session],
) -> None:
    """
    the superseded worker and the current owner record from two connections at the same instant
    :param sessions: factory handing out independent connections
    """
    delivery_id = seed_one(sessions)
    reclaimed_at = NOW + timedelta(seconds=LEASE + 1)

    with sessions() as setup_a, sessions() as setup_b:
        job_a = claim(setup_a, now=NOW)
        job_b = claim(setup_b, now=reclaimed_at)
        token_a, token_b = job_a.claim_token, job_b.claim_token

    barrier = threading.Barrier(2)

    def record(token: str | None, moment: datetime) -> storage.CompletedAttempt:
        barrier.wait()
        with sessions() as session:
            job = session.get(models.WebhookDelivery, delivery_id)
            assert job is not None
            return storage.complete_attempt(
                session, job, SUCCESS, now=moment, policy=POLICY, claim_token=token
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(record, token_a, NOW),
            pool.submit(record, token_b, reclaimed_at),
        ]
        stale, owner = (future.result() for future in futures)

    assert [stale.owned, owner.owned] == [False, True]
    assert attempt_numbers(sessions, delivery_id) == [1, 2], "an attempt number was reused"
    settled = read(sessions, delivery_id)
    assert settled.attempts == 2
    assert settled.state == DeliveryState.DELIVERED
    assert settled.cycle_attempts == 1


def test_reclaiming_after_a_superseded_completion_still_works(
    sessions: sessionmaker[Session],
) -> None:
    """
    refusing a superseded completion must not strand the delivery in processing
    :param sessions: factory handing out independent connections
    """
    delivery_id = seed_one(sessions)
    reclaimed_at = NOW + timedelta(seconds=LEASE + 1)

    with sessions() as worker_a, sessions() as worker_b:
        job_a = claim(worker_a, now=NOW)
        claim(worker_b, now=reclaimed_at)
        storage.complete_attempt(
            worker_a, job_a, SUCCESS, now=NOW, policy=POLICY, claim_token=job_a.claim_token
        )

    # B is now abandoned in its turn, and the ordinary expired-lease recovery has
    # to keep working over a row a superseded worker has already written to.
    with sessions() as worker_c:
        recovered = claim(worker_c, now=reclaimed_at + timedelta(seconds=LEASE + 1))
        assert recovered.id == delivery_id

        outcome = storage.complete_attempt(
            worker_c,
            recovered,
            SUCCESS,
            now=reclaimed_at + timedelta(seconds=LEASE + 1),
            policy=POLICY,
            claim_token=recovered.claim_token,
        )

    assert outcome.owned is True
    assert read(sessions, delivery_id).state == DeliveryState.DELIVERED
    assert attempt_numbers(sessions, delivery_id) == [1, 2]
