"""
persistence operations, the only place queries are written

Most functions here leave the commit to the caller, so a request handler decides
when its work becomes durable and a failure anywhere before that commit leaves
the database untouched. A few own their transaction and say so:
:func:`store_submission`, because it writes a submission and the obligation to
deliver it as one atomic unit and must roll both back together to settle an
idempotency race; :func:`claim_due_deliveries`, because a claim is only worth
anything once it is committed; :func:`complete_attempt`, because the audit
record and the state it justifies have to land together; :func:`update_endpoint`
and :func:`requeue_failed_delivery`, because each is the whole of what its
management request came to do; the two management key writes,
:func:`revoke_management_key` and :func:`record_management_key_use`, for the
same reason; the two rate limit operations, :func:`consume_rate_limit` and
:func:`delete_expired_rate_limit_counters`, because abuse accounting has to
outlive the request it was accounting for; and
:func:`delete_expired_submissions`, because a retention sweep is deliberately
many small committed batches rather than one long transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    delete,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session

from hymical_forms import models
from hymical_forms.ingestion import Submission
from hymical_forms.ratelimit import Limiter
from hymical_forms.retention import PROTECTED_DELIVERY_STATES
from hymical_forms.webhooks import (
    DeliveryOutcome,
    DeliveryResult,
    DeliveryState,
    RetryPolicy,
    WebhookTarget,
    is_retryable,
    new_delivery_attempt_id,
    new_webhook_delivery_id,
)

# The three tables management routes page through. Each is keyed by an opaque
# identifier and carries a timestamp it is ordered by, which is all cursor
# pagination here asks of a table. Which timestamp differs, so callers name it.
_Paged = TypeVar("_Paged", models.Endpoint, models.WebhookDelivery, models.Submission)

# Any submission select, whatever it happens to be selecting. The submission
# filters are applied to a listing, an export and a bounded count alike, and
# narrowing a select never changes what it returns.
_Statement = TypeVar("_Statement", bound=Select[Any])

# How many rows a streamed export pulls from the server at a time. Bounded so
# that an export builds its response as it goes rather than materialising every
# matching row before the first byte is written.
EXPORT_FETCH_SIZE = 200


class UnknownCursor(Exception):
    """
    raised when a pagination cursor does not name a row to continue from
    """

    def __init__(self, cursor: str) -> None:
        """
        record the cursor that could not be resolved
        :param cursor: the opaque cursor the caller asked to continue from
        """
        super().__init__("the pagination cursor does not name a known row")
        self.cursor = cursor


class EndpointAlreadyExists(Exception):
    """
    raised when an endpoint ID is already taken
    """

    def __init__(self, endpoint_id: str) -> None:
        """
        record which endpoint ID was already in use
        :param endpoint_id: the identifier that collided
        """
        super().__init__(f"endpoint {endpoint_id!r} already exists")
        self.endpoint_id = endpoint_id


def create_endpoint(
    session: Session,
    *,
    endpoint_id: str,
    name: str,
    is_active: bool,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> models.Endpoint:
    """
    add an endpoint, failing if the identifier is taken
    :param session: the session to add the endpoint through
    :param endpoint_id: the public identifier the endpoint will answer on
    :param name: human-readable label for the endpoint
    :param is_active: whether the endpoint should accept submissions straight away
    :param webhook_url: destination to deliver submissions to, or None for no webhook
    :param webhook_secret: signing secret for that destination, set only alongside a URL
    :returns: the pending endpoint, not yet committed
    :raises EndpointAlreadyExists: if an endpoint already holds that identifier
    """
    endpoint = models.Endpoint(
        id=endpoint_id,
        name=name,
        is_active=is_active,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
    )
    session.add(endpoint)
    try:
        # Flushing here turns the unique violation into a catchable error while
        # the caller can still react, rather than at an opaque commit later.
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise EndpointAlreadyExists(endpoint_id) from exc
    return endpoint


def get_endpoint(session: Session, endpoint_id: str) -> models.Endpoint | None:
    """
    look an endpoint up by its public identifier
    :param session: the session to query through
    :param endpoint_id: the public identifier to resolve
    :returns: the endpoint, or None if no endpoint holds that identifier
    """
    return session.get(models.Endpoint, endpoint_id)


def list_endpoints(
    session: Session, *, limit: int, after: str | None = None
) -> list[models.Endpoint]:
    """
    read one page of endpoints, newest first
    :param session: the session to query through
    :param limit: the most endpoints to return
    :param after: identifier of the last endpoint on the previous page, or None to start
    :returns: the page, at most ``limit`` long
    :raises UnknownCursor: if ``after`` does not name an existing endpoint
    """
    endpoint = models.Endpoint
    statement = select(endpoint).order_by(endpoint.created_at.desc(), endpoint.id.desc())
    if after is not None:
        statement = statement.where(
            _page_after(session, endpoint, after, ordered_by=endpoint.created_at)
        )
    return list(session.scalars(statement.limit(limit)))


def get_endpoint_for_update(session: Session, endpoint_id: str) -> models.Endpoint | None:
    """
    read an endpoint with the intention of changing it in the same transaction
    :param session: the session to query through
    :param endpoint_id: the public identifier to resolve
    :returns: the endpoint, or None if no endpoint holds that identifier
    """
    # Locked on PostgreSQL, because two operators changing one endpoint's webhook
    # at the same moment would otherwise both read the old destination, both mint
    # a secret, and leave one of them holding a secret this service never stored.
    # SQLite has no row locking and serialises writers anyway; it is not a
    # production target.
    statement = select(models.Endpoint).where(models.Endpoint.id == endpoint_id)
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalars(statement).one_or_none()


def update_endpoint(
    session: Session,
    endpoint: models.Endpoint,
    *,
    name: str,
    is_active: bool,
    webhook_url: str | None,
    webhook_secret: str | None,
) -> models.Endpoint:
    """
    write a resolved endpoint configuration and commit it
    :param session: the session to write through
    :param endpoint: the endpoint being changed, already loaded in this transaction
    :param name: the label the endpoint should end up with
    :param is_active: whether the endpoint should accept submissions
    :param webhook_url: the destination it should end up with, or None for no webhook
    :param webhook_secret: the signing secret for that destination, paired with the URL
    :returns: the endpoint, changed and committed
    """
    # Every value is resolved by the caller, so the decision about what "unchanged"
    # means for a partial update stays in one place and this function has no
    # opinion about it. Deliveries already queued are untouched on purpose: they
    # snapshotted their destination and secret when the submission was accepted.
    endpoint.name = name
    endpoint.is_active = is_active
    endpoint.webhook_url = webhook_url
    endpoint.webhook_secret = webhook_secret
    session.commit()
    return endpoint


class IdempotencyKeyReused(Exception):
    """
    raised when an idempotency key was already used for different content
    """

    def __init__(self, endpoint_id: str, idempotency_key: str) -> None:
        """
        record which key was reused, and where
        :param endpoint_id: the endpoint the key is scoped to
        :param idempotency_key: the key that was already spent on other content
        """
        super().__init__(f"idempotency key already used on endpoint {endpoint_id!r}")
        self.endpoint_id = endpoint_id
        self.idempotency_key = idempotency_key


@dataclass(frozen=True, slots=True)
class StoredSubmission:
    """
    the outcome of storing a submission
    """

    submission: Submission
    replayed: bool


def find_by_idempotency_key(
    session: Session, endpoint_id: str, idempotency_key: str
) -> models.Submission | None:
    """
    look up the submission a key was already spent on
    :param session: the session to query through
    :param endpoint_id: the endpoint the key is scoped to
    :param idempotency_key: the key to resolve
    :returns: the earlier submission, or None if the key is unused
    """
    return session.scalars(
        select(models.Submission).where(
            models.Submission.endpoint_id == endpoint_id,
            models.Submission.idempotency_key == idempotency_key,
        )
    ).one_or_none()


def store_submission(
    session: Session,
    submission: Submission,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    payload_fingerprint: str | None = None,
    webhook: WebhookTarget | None = None,
) -> StoredSubmission:
    """
    store a submission and the obligation to deliver it, or resolve to an earlier one
    :param session: the session to write through
    :param submission: the validated domain submission to store
    :param now: the instant the submission was accepted
    :param idempotency_key: the client's retry key, or None if it sent none
    :param payload_fingerprint: digest of the submitted content, required alongside a key
    :param webhook: the destination owed delivery, or None if the endpoint has no webhook
    :returns: the stored submission and whether it came from an earlier attempt
    :raises IdempotencyKeyReused: if the key was already used for different content
    """
    # The submission and its delivery obligation go in as one transaction. That
    # is the whole reliability claim: once this commits, a crash cannot leave a
    # submission that was acknowledged with nothing durable saying delivery is
    # still owed, and it cannot leave delivery work for a submission that never
    # existed either.
    if idempotency_key is None:
        _add_submission(session, submission, now=now, webhook=webhook)
        session.commit()
        return StoredSubmission(submission, replayed=False)

    # Fast path for the ordinary retry, where the first attempt already landed.
    existing = find_by_idempotency_key(session, submission.endpoint_id, idempotency_key)
    if existing is not None:
        return StoredSubmission(_settle(existing, payload_fingerprint), replayed=True)

    _add_submission(
        session,
        submission,
        now=now,
        webhook=webhook,
        idempotency_key=idempotency_key,
        payload_fingerprint=payload_fingerprint,
    )
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request inserted the same key between the lookup above and
        # this commit. The unique constraint is what caught it, which is the whole
        # point: the lookup is an optimisation, never the guarantee.
        #
        # The rollback is mandatory. A session left holding a failed flush refuses
        # every later query with PendingRollbackError, so the read below would
        # fail rather than find the winner. Rolling back also discards this
        # request's submission and its delivery together, leaving exactly the one
        # submission and the one delivery the winner committed.
        session.rollback()
        existing = find_by_idempotency_key(session, submission.endpoint_id, idempotency_key)
        if existing is None:
            # The violation was something other than the idempotency constraint,
            # so it is not ours to interpret and must not be reported as success.
            raise
        return StoredSubmission(_settle(existing, payload_fingerprint), replayed=True)

    return StoredSubmission(submission, replayed=False)


def _add_submission(
    session: Session,
    submission: Submission,
    *,
    now: datetime,
    webhook: WebhookTarget | None,
    idempotency_key: str | None = None,
    payload_fingerprint: str | None = None,
) -> None:
    """
    stage a submission and, if one is owed, its delivery, without committing
    :param session: the session to add through
    :param submission: the validated domain submission to store
    :param now: the instant the submission was accepted
    :param webhook: the destination owed delivery, or None if the endpoint has no webhook
    :param idempotency_key: the client's retry key, or None if it sent none
    :param payload_fingerprint: digest of the submitted content, set only alongside a key
    """
    session.add(
        models.Submission.from_domain(
            submission,
            idempotency_key=idempotency_key,
            payload_fingerprint=payload_fingerprint,
        )
    )
    if webhook is None:
        return

    session.add(
        models.WebhookDelivery(
            id=new_webhook_delivery_id(),
            submission_id=submission.id,
            endpoint_id=submission.endpoint_id,
            destination_url=webhook.url,
            signing_secret=webhook.secret,
            state=DeliveryState.PENDING,
            attempts=0,
            # Due straight away: the first attempt is not delayed, it is simply
            # made by a worker rather than by the request that caused it.
            next_attempt_at=now,
            created_at=now,
        )
    )


@dataclass(frozen=True, slots=True)
class SubmissionFilter:
    """
    the bounded set of stored submissions a management read is asking for
    """

    # Both bounds are exclusive, which is the contract the API documents. Kept
    # together in one value so that the listing, the export and the count that
    # guards the export cannot end up asking three slightly different questions.
    endpoint_id: str | None = None
    received_after: datetime | None = None
    received_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    """
    a stored submission together with the delivery it owes, if it owes one
    """

    # There is at most one delivery per submission, enforced by a unique
    # constraint, so this is a value rather than a list. It is None for a
    # submission whose endpoint has no webhook.
    submission: models.Submission
    delivery: models.WebhookDelivery | None


def list_submissions(
    session: Session,
    *,
    filters: SubmissionFilter,
    limit: int,
    after: str | None = None,
) -> list[SubmissionRecord]:
    """
    read one page of stored submissions, newest first
    :param session: the session to query through
    :param filters: the endpoint and time bounds to narrow the page to
    :param limit: the most submissions to return
    :param after: identifier of the last submission on the previous page, or None to start
    :returns: the page, at most ``limit`` long
    :raises UnknownCursor: if ``after`` does not name an existing submission
    """
    submission = models.Submission
    delivery = models.WebhookDelivery
    statement = (
        # An outer join, because a submission to an endpoint with no webhook owes
        # no delivery and must still be listed. One join rather than a lookup per
        # row, and it rides the unique constraint on the delivery's submission.
        select(submission, delivery)
        .outerjoin(delivery, delivery.submission_id == submission.id)
        .order_by(submission.received_at.desc(), submission.id.desc())
    )
    statement = _narrowed(statement, filters)
    if after is not None:
        statement = statement.where(
            _page_after(session, submission, after, ordered_by=submission.received_at)
        )

    return [SubmissionRecord(row, owed) for row, owed in session.execute(statement.limit(limit))]


def get_submission(session: Session, submission_id: str) -> SubmissionRecord | None:
    """
    look one stored submission up by its identifier
    :param session: the session to query through
    :param submission_id: the identifier to resolve
    :returns: the submission and its delivery, or None if no submission holds that identifier
    """
    submission = models.Submission
    delivery = models.WebhookDelivery
    row = session.execute(
        select(submission, delivery)
        .outerjoin(delivery, delivery.submission_id == submission.id)
        .where(submission.id == submission_id)
    ).one_or_none()
    return SubmissionRecord(row[0], row[1]) if row is not None else None


def count_submissions(session: Session, *, filters: SubmissionFilter, ceiling: int) -> int:
    """
    count the matching submissions, giving up once a ceiling is reached
    :param session: the session to query through
    :param filters: the endpoint and time bounds to count within
    :param ceiling: the most rows to count before stopping
    :returns: the number of matches, or ``ceiling`` when there are at least that many
    """
    # Counting through a bounded subquery rather than counting the table, so a
    # filter matching millions costs a walk of ``ceiling`` index entries and not
    # a walk of everything. The caller only needs to know whether the export fits.
    bounded = _narrowed(select(models.Submission.id), filters).limit(ceiling).subquery()
    return cast(int, session.scalar(select(func.count()).select_from(bounded)))


def stream_submissions(
    session: Session, *, filters: SubmissionFilter, limit: int
) -> Iterator[models.Submission]:
    """
    read the matching submissions in export order, a chunk at a time
    :param session: the session to query through
    :param filters: the endpoint and time bounds to export within
    :param limit: the most submissions to yield, whatever the filter matches
    :returns: an iterator over the matching submissions, newest first
    """
    # ``yield_per`` is what makes this a stream rather than a list wearing an
    # iterator's clothes: rows arrive from the server in chunks, so an export
    # writes its first bytes without every matching row being in memory. The
    # limit is still applied, so even a runaway cursor is bounded.
    submission = models.Submission
    statement = _narrowed(
        select(submission).order_by(submission.received_at.desc(), submission.id.desc()),
        filters,
    ).limit(limit)
    return iter(session.scalars(statement.execution_options(yield_per=EXPORT_FETCH_SIZE)))


def count_expired_submissions(session: Session, *, before: datetime) -> int:
    """
    count the submissions a retention sweep would delete
    :param session: the session to query through
    :param before: submissions received strictly before this instant are eligible
    :returns: how many submissions are eligible for deletion
    """
    return cast(
        int,
        session.scalar(
            select(func.count()).select_from(models.Submission).where(_expired_condition(before))
        ),
    )


def delete_expired_submissions(
    session: Session, *, before: datetime, batch_size: int, max_batches: int
) -> int:
    """
    delete eligible submissions in committed batches until none are left
    :param session: the session to write through
    :param before: submissions received strictly before this instant are eligible
    :param batch_size: the most submissions to remove in one transaction
    :param max_batches: the most batches this run will do before stopping
    :returns: how many submissions were deleted
    """
    # Many short transactions rather than one long one. A single DELETE over a
    # large backlog would hold locks on every row it touched for as long as the
    # whole sweep took; this way each batch is committed and released, and a run
    # that is interrupted has still durably removed everything it reported.
    #
    # The eligible identifiers are read first and deleted by identifier, so the
    # delete is a primary key lookup and the eligibility test is evaluated once
    # rather than being re-planned inside a delete.
    removed = 0
    for _ in range(max_batches):
        ids = list(
            session.scalars(
                select(models.Submission.id).where(_expired_condition(before)).limit(batch_size)
            )
        )
        if not ids:
            break

        # The delivery and the attempts that referenced these submissions are not
        # touched. Their foreign keys are ``ON DELETE SET NULL``, so the database
        # unlinks them and leaves the operational history standing.
        result = cast(
            "CursorResult[Any]",
            session.execute(
                delete(models.Submission)
                .where(models.Submission.id.in_(ids))
                .execution_options(synchronize_session=False)
            ),
        )
        session.commit()
        removed += result.rowcount
        if len(ids) < batch_size:
            break

    return removed


def _expired_condition(before: datetime) -> ColumnElement[bool]:
    """
    build the test for a submission a retention sweep may delete
    :param before: submissions received strictly before this instant are eligible
    :returns: a SQL condition matching only submissions nothing still needs
    """
    # Old enough, and not carrying a delivery that could still be attempted. The
    # payload is built from the submission at send time, so a pending, processing
    # or replayable failed delivery would be left with nothing to send. A
    # submission with no delivery, and one whose delivery is already delivered,
    # both pass, because nothing will read their fields again.
    delivery = models.WebhookDelivery
    still_needed = select(delivery.id).where(
        delivery.submission_id == models.Submission.id,
        delivery.state.in_(PROTECTED_DELIVERY_STATES),
    )
    return and_(models.Submission.received_at < before, ~still_needed.exists())


def _narrowed(statement: _Statement, filters: SubmissionFilter) -> _Statement:
    """
    apply the endpoint and time bounds a management read asked for
    :param statement: the select to narrow
    :param filters: the endpoint and time bounds to apply
    :returns: the same select, narrowed
    """
    # Both time bounds are strict. An operator paging forward by passing the last
    # timestamp back as ``received_after`` gets the next submissions rather than
    # the one they already have, which is the behaviour the API documents.
    submission = models.Submission
    if filters.endpoint_id is not None:
        statement = statement.where(submission.endpoint_id == filters.endpoint_id)
    if filters.received_after is not None:
        statement = statement.where(submission.received_at > filters.received_after)
    if filters.received_before is not None:
        statement = statement.where(submission.received_at < filters.received_before)
    return statement


def due_condition(now: datetime) -> ColumnElement[bool]:
    """
    build the test for a delivery a worker is allowed to pick up
    :param now: the instant to judge dueness against
    :returns: a SQL condition matching claimable deliveries
    """
    # Two ways to be claimable: waiting and due, or claimed by a worker whose
    # lease has run out. The second is what recovers work from a worker that died
    # holding a job, rather than leaving it stuck in ``processing`` forever.
    #
    # A delivery with no submission is never due, whichever state it is in. The
    # payload is built from the submission at send time, so a delivery without
    # one has nothing to send. Retention can only unlink a delivery that has
    # already been delivered, which this condition excludes anyway; the test is
    # here so that the guarantee is the query's rather than the sweep's.
    delivery = models.WebhookDelivery
    return and_(
        delivery.submission_id.is_not(None),
        or_(
            and_(delivery.state == DeliveryState.PENDING, delivery.next_attempt_at <= now),
            and_(delivery.state == DeliveryState.PROCESSING, delivery.claim_expires_at <= now),
        ),
    )


def claim_due_deliveries(
    session: Session, *, now: datetime, lease_seconds: float, limit: int
) -> list[models.WebhookDelivery]:
    """
    take ownership of up to a few due deliveries, in one committed transaction
    :param session: the session to claim through
    :param now: the instant to judge dueness against
    :param lease_seconds: how long the claim protects a delivery from other workers
    :param limit: the most deliveries to claim at once
    :returns: the deliveries this worker now owns
    """
    delivery = models.WebhookDelivery
    due = due_condition(now)

    statement = select(delivery).where(due).order_by(delivery.next_attempt_at).limit(limit)
    if session.get_bind().dialect.name == "postgresql":
        # PostgreSQL can hand each worker a different set of rows outright, which
        # is the real answer to two workers scanning at once. SKIP LOCKED means a
        # busy row is passed over rather than waited on.
        statement = statement.with_for_update(skip_locked=True)
    candidates = list(session.scalars(statement))

    claimed: list[models.WebhookDelivery] = []
    expires_at = now + timedelta(seconds=lease_seconds)
    for candidate in candidates:
        # The conditional update is the guarantee on backends without row locking:
        # whoever gets there first flips the row out of the due condition, and the
        # loser's update matches nothing. Redundant under SKIP LOCKED, and cheap.
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(delivery)
                .where(delivery.id == candidate.id)
                .where(due)
                .values(state=DeliveryState.PROCESSING, claim_expires_at=expires_at)
                .execution_options(synchronize_session="fetch")
            ),
        )
        if result.rowcount == 1:
            claimed.append(candidate)

    session.commit()
    return claimed


def load_submissions(
    session: Session, deliveries: list[models.WebhookDelivery]
) -> dict[str, Submission]:
    """
    load the submission each claimed delivery is carrying
    :param session: the session to query through
    :param deliveries: the deliveries a worker has just claimed
    :returns: the submissions in domain form, keyed by the delivery that carries each one
    """
    # Keyed by delivery rather than by submission, because the delivery is what
    # the caller is holding and because the link between them is now nullable.
    # Nothing due can have lost its submission, since only a delivered delivery
    # is ever unlinked and a delivered delivery is not claimable, so this filter
    # states that rather than relying on it.
    carried = {job.id: job.submission_id for job in deliveries if job.submission_id is not None}

    # One query for the batch rather than a lookup per delivery.
    rows = {
        row.id: row.to_domain()
        for row in session.scalars(
            select(models.Submission).where(models.Submission.id.in_(carried.values()))
        )
    }
    return {job_id: rows[submission_id] for job_id, submission_id in carried.items()}


def complete_attempt(
    session: Session,
    delivery: models.WebhookDelivery,
    result: DeliveryResult,
    *,
    now: datetime,
    policy: RetryPolicy,
) -> models.DeliveryAttempt:
    """
    record one outbound request and move the delivery to whatever it earned
    :param session: the session to write through
    :param delivery: the delivery the attempt was made for
    :param result: what the attempt produced
    :param now: the instant the attempt finished
    :param policy: how many attempts are allowed and how long to wait between them
    :returns: the committed attempt record
    """
    # The audit row and the state it justifies are written together, so the
    # history can never disagree with the job about how many attempts happened.
    #
    # Two counters, because they answer two different questions. The attempt is
    # numbered from the lifetime total, so a number is never reused however often
    # the delivery has been replayed. The retry allowance is measured against the
    # current cycle, so a replayed delivery gets a whole schedule again rather
    # than failing immediately on a total that is already spent. Until something
    # is replayed the two are the same number.
    attempt_number = delivery.attempts + 1
    cycle_attempt = delivery.cycle_attempts + 1
    attempt = models.DeliveryAttempt(
        id=new_delivery_attempt_id(),
        delivery_id=delivery.id,
        submission_id=delivery.submission_id,
        attempt_number=attempt_number,
        destination_url=delivery.destination_url,
        attempted_at=now,
        outcome=str(result.outcome),
        response_status=result.response_status,
        error=result.error,
    )
    session.add(attempt)

    delivery.attempts = attempt_number
    delivery.cycle_attempts = cycle_attempt
    delivery.claim_expires_at = None

    if result.outcome is DeliveryOutcome.SUCCEEDED:
        delivery.state = DeliveryState.DELIVERED
        delivery.completed_at = now
    elif is_retryable(result) and not policy.is_exhausted(cycle_attempt):
        delivery.state = DeliveryState.PENDING
        delivery.next_attempt_at = now + policy.delay_after(cycle_attempt)
    else:
        # Either the receiver said something repeating will not fix, or the
        # allowance ran out. Either way this is the last word on the delivery.
        delivery.state = DeliveryState.FAILED
        delivery.completed_at = now

    session.commit()
    return attempt


def list_deliveries(
    session: Session,
    *,
    limit: int,
    after: str | None = None,
    endpoint_id: str | None = None,
    state: str | None = None,
) -> list[models.WebhookDelivery]:
    """
    read one page of the delivery queue, newest first
    :param session: the session to query through
    :param limit: the most deliveries to return
    :param after: identifier of the last delivery on the previous page, or None to start
    :param endpoint_id: only deliveries for this endpoint, or None for every endpoint
    :param state: only deliveries in this state, or None for every state
    :returns: the page, at most ``limit`` long
    :raises UnknownCursor: if ``after`` does not name an existing delivery
    """
    # No join. The endpoint is a column on the delivery, so a delivery whose
    # submission has been retained away is still listed, still says which
    # endpoint it belonged to, and is still reachable through this filter.
    delivery = models.WebhookDelivery
    statement = select(delivery).order_by(delivery.created_at.desc(), delivery.id.desc())
    if endpoint_id is not None:
        statement = statement.where(delivery.endpoint_id == endpoint_id)
    if state is not None:
        statement = statement.where(delivery.state == state)
    if after is not None:
        statement = statement.where(
            _page_after(session, delivery, after, ordered_by=delivery.created_at)
        )

    return list(session.scalars(statement.limit(limit)))


def get_delivery(session: Session, delivery_id: str) -> models.WebhookDelivery | None:
    """
    look one delivery up by its identifier
    :param session: the session to query through
    :param delivery_id: the identifier to resolve
    :returns: the delivery, or None if no delivery holds that identifier
    """
    return session.get(models.WebhookDelivery, delivery_id)


def list_delivery_attempts(session: Session, delivery_id: str) -> list[models.DeliveryAttempt]:
    """
    read the ordered attempt history of one delivery
    :param session: the session to query through
    :param delivery_id: the delivery whose history to read
    :returns: every recorded attempt, lowest attempt number first
    """
    # Attempt numbers never repeat within a delivery, even across a manual replay,
    # so ordering by one is already total. The identifier is a tie-break that
    # cannot be reached rather than one that is expected to matter.
    attempt = models.DeliveryAttempt
    return list(
        session.scalars(
            select(attempt)
            .where(attempt.delivery_id == delivery_id)
            .order_by(attempt.attempt_number, attempt.id)
        )
    )


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """
    what a manual replay request did, and what the delivery looks like now
    """

    # ``record`` is None only when the delivery does not exist. ``requeued`` is
    # False for a delivery that was not failed, which includes the loser of two
    # simultaneous replays: it reads back the pending delivery the winner made.
    record: models.WebhookDelivery | None
    requeued: bool


def requeue_failed_delivery(session: Session, delivery_id: str, *, now: datetime) -> ReplayOutcome:
    """
    put a terminally failed delivery back in the queue for the worker to claim
    :param session: the session to write through
    :param delivery_id: the delivery to requeue
    :param now: the instant the delivery should become due again
    :returns: the resulting delivery and whether this request is what requeued it
    """
    # The transition is one conditional UPDATE, so the database decides who wins.
    # Reading the state, judging it in Python and then writing would let two
    # simultaneous replays both pass the check and both reset the retry cycle,
    # which is exactly the duplicated work this is meant to prevent.
    #
    # Only the queueing state is touched. The snapshotted destination and signing
    # secret, the submission, the lifetime attempt count and every recorded
    # attempt are left exactly as they are: a replay resumes a delivery, it does
    # not start a new one. Clearing ``cycle_attempts`` is what grants the fresh
    # retry allowance, and clearing ``completed_at`` is required by the check
    # constraint that says a delivery is finished exactly when it says it is.
    delivery = models.WebhookDelivery
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(delivery)
            .where(delivery.id == delivery_id)
            .where(delivery.state == DeliveryState.FAILED)
            .values(
                state=DeliveryState.PENDING,
                cycle_attempts=0,
                next_attempt_at=now,
                claim_expires_at=None,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    session.commit()

    # Read after the commit, so both the winner and the loser describe the state
    # the database actually settled on rather than the one they hoped for.
    return ReplayOutcome(get_delivery(session, delivery_id), requeued=result.rowcount == 1)


def create_management_key(
    session: Session,
    *,
    key_id: str,
    name: str,
    display_prefix: str,
    key_digest: str,
    now: datetime,
) -> models.ManagementApiKey:
    """
    add a management API key, storing only its safe representation
    :param session: the session to add the key through
    :param key_id: the non-secret identifier the key will be administered by
    :param name: human-readable label for the key
    :param display_prefix: the non-secret fragment shown in a listing
    :param key_digest: digest of the credential, which is never stored itself
    :param now: the instant the key was created
    :returns: the pending key, not yet committed
    """
    # The credential is not a parameter here, and that is the point: this
    # function could not persist it even if a caller wanted it to.
    key = models.ManagementApiKey(
        id=key_id,
        name=name,
        display_prefix=display_prefix,
        key_digest=key_digest,
        created_at=now,
    )
    session.add(key)
    session.flush()
    return key


def find_management_key_by_digest(
    session: Session, key_digest: str
) -> models.ManagementApiKey | None:
    """
    look up the key a supplied credential digests to
    :param session: the session to query through
    :param key_digest: digest of the credential the client sent
    :returns: the key, or None if no key digests to that value
    """
    # Revoked keys are returned too. Whether a key still authenticates is the
    # caller's decision to make and to answer for, and filtering here would hide
    # the distinction from the one place that has to be explicit about it.
    return session.scalars(
        select(models.ManagementApiKey).where(models.ManagementApiKey.key_digest == key_digest)
    ).one_or_none()


def get_management_key(session: Session, key_id: str) -> models.ManagementApiKey | None:
    """
    look a management key up by its non-secret identifier
    :param session: the session to query through
    :param key_id: the identifier to resolve
    :returns: the key, or None if no key holds that identifier
    """
    return session.get(models.ManagementApiKey, key_id)


def list_management_keys(session: Session) -> list[models.ManagementApiKey]:
    """
    read every management key, newest first
    :param session: the session to query through
    :returns: the keys, carrying no credential material
    """
    return list(
        session.scalars(
            select(models.ManagementApiKey).order_by(models.ManagementApiKey.created_at.desc())
        )
    )


def revoke_management_key(
    session: Session, key_id: str, *, now: datetime
) -> models.ManagementApiKey | None:
    """
    withdraw a management key without discarding what it was
    :param session: the session to write through
    :param key_id: the identifier of the key to revoke
    :param now: the instant the revocation takes effect
    :returns: the key, revoked and committed, or None if no such key exists
    """
    key = session.get(models.ManagementApiKey, key_id)
    if key is None:
        return None
    # Idempotent, and the first revocation is the one that counts: revoking twice
    # must not quietly move the moment the credential stopped being valid.
    if key.revoked_at is None:
        key.revoked_at = now
        session.commit()
    return key


def record_management_key_use(session: Session, key_id: str, *, now: datetime) -> None:
    """
    note that a key authenticated a request
    :param session: the session to write through
    :param key_id: the key that authenticated
    :param now: the instant the request was authenticated
    """
    # An UPDATE rather than a load-and-set, so this costs one statement and does
    # not put an object in the caller's session that a later rollback would
    # expire underneath them.
    session.execute(
        update(models.ManagementApiKey)
        .where(models.ManagementApiKey.id == key_id)
        .values(last_used_at=now)
    )
    session.commit()


class UnsupportedRateLimitBackend(Exception):
    """
    raised when the configured database cannot perform an atomic counter upsert
    """

    def __init__(self, dialect: str) -> None:
        """
        record which backend was asked to enforce a rate limit
        :param dialect: the SQLAlchemy dialect name the session is bound to
        """
        super().__init__(
            f"rate limiting needs PostgreSQL or SQLite, not {dialect!r}, "
            "because the counter is incremented with an upsert"
        )
        self.dialect = dialect


def consume_rate_limit(
    session: Session,
    *,
    limiter: Limiter,
    subject: str,
    window_start: datetime,
) -> int:
    """
    spend one unit of a subject's budget for one window, atomically
    :param session: the session to write through
    :param limiter: which budget is being drawn from
    :param subject: the value that budget is keyed by
    :param window_start: the start of the fixed window being counted in
    :returns: how many attempts this subject has now made inside that window
    :raises UnsupportedRateLimitBackend: if the session is bound to another database
    """
    # One statement decides everything. Reading the counter, comparing it in
    # Python and writing it back would let two requests that arrive together both
    # read the same value, both find room, and both pass, which is precisely the
    # hole a rate limiter exists to close. Here the database inserts the row or
    # increments the existing one under its own row lock, and hands back the value
    # it settled on, so two simultaneous requests get two different numbers and at
    # most one of them can be the last one under the limit.
    #
    # ``ON CONFLICT DO UPDATE`` rather than a lock-then-update, because the row
    # for a brand new subject does not exist yet and two requests racing to create
    # it have nothing to lock. The upsert makes the create and the increment the
    # same operation, so the first request of a window is settled by the primary
    # key rather than by whoever inserted first.
    counter = models.RateLimitCounter
    upsert: Any = _upsert_for(session)
    statement = (
        upsert(counter)
        .values(
            limiter=str(limiter),
            subject=subject,
            window_start=window_start,
            attempts=1,
        )
        .on_conflict_do_update(
            index_elements=["limiter", "subject", "window_start"],
            # The unqualified column on the right is the stored row's value, not
            # the one this statement proposed, which is what makes this an
            # increment rather than an overwrite.
            set_={"attempts": counter.attempts + 1},
        )
        .returning(counter.attempts)
    )
    used = cast(int, session.scalars(statement).one())

    # Committed here, and deliberately not left to the caller. The decision has to
    # survive whatever the request does next: a submission that is refused, fails
    # validation, or loses an idempotency race rolls its own work back, and abuse
    # accounting that rolled back with it would let an attacker send unlimited
    # traffic as long as every request was invalid.
    session.commit()
    return used


def delete_expired_rate_limit_counters(session: Session, *, before: datetime) -> int:
    """
    remove counters for windows old enough that nothing will consult them again
    :param session: the session to write through
    :param before: counters whose window starts strictly before this instant are removed
    :returns: how many counters were removed
    """
    # A range over the indexed window column, so this is a bounded delete rather
    # than a scan of the table. The caller chooses a cutoff several windows in the
    # past, so a sweep can never take a window that is still being counted in.
    counter = models.RateLimitCounter
    result = cast(
        "CursorResult[Any]",
        session.execute(
            delete(counter)
            .where(counter.window_start < before)
            .execution_options(synchronize_session=False)
        ),
    )
    session.commit()
    return result.rowcount


def _upsert_for(session: Session) -> Any:
    """
    pick the dialect-specific insert that can express an atomic increment
    :param session: the session whose backend the statement will run against
    :returns: the dialect's ``insert`` construct
    :raises UnsupportedRateLimitBackend: if the backend is neither PostgreSQL nor SQLite
    """
    # ``ON CONFLICT`` is not in the generic construct, so the dialect has to be
    # named. Anything else is refused rather than silently falling back to a
    # read-compare-write that would not be atomic.
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise UnsupportedRateLimitBackend(dialect)


def _page_after(
    session: Session,
    model: type[_Paged],
    cursor: str,
    *,
    ordered_by: Mapped[datetime],
) -> ColumnElement[bool]:
    """
    build the test for rows that come after a cursor, in newest-first order
    :param session: the session to resolve the cursor through
    :param model: the table being paged
    :param cursor: the identifier of the last row on the previous page
    :param ordered_by: the timestamp column the page is ordered by, newest first
    :returns: a SQL condition matching only the rows that follow it
    :raises UnknownCursor: if the cursor does not name an existing row
    """
    # The anchor's ordering value is read through the column the caller named
    # rather than off a loaded row, so this works for any table without knowing
    # what its timestamp is called.
    anchor = session.execute(select(ordered_by, model.id).where(model.id == cursor)).one_or_none()
    if anchor is None:
        # Refused rather than treated as the first page, so a caller that pages
        # past a row somebody deleted learns about it instead of silently
        # starting again from the top.
        raise UnknownCursor(cursor)

    # A row-value comparison, so the tie-break and the ordering are one expression
    # rather than two that have to be kept in agreement. Both timestamps and both
    # identifiers are compared, which is what makes a page boundary total even
    # when several rows were created in the same transaction.
    return tuple_(ordered_by, model.id) < (anchor[0], anchor[1])


def _settle(existing: models.Submission, payload_fingerprint: str | None) -> Submission:
    """
    decide whether an earlier submission is a replay of this one or a clash
    :param existing: the submission the key was already spent on
    :param payload_fingerprint: digest of the content submitted this time
    :returns: the earlier submission, when the content matches
    :raises IdempotencyKeyReused: if the content differs from the earlier attempt
    """
    if existing.payload_fingerprint != payload_fingerprint:
        raise IdempotencyKeyReused(existing.endpoint_id, str(existing.idempotency_key))
    return existing.to_domain()
