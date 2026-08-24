"""
persistence operations, the only place queries are written

Most functions here leave the commit to the caller, so a request handler decides
when its work becomes durable and a failure anywhere before that commit leaves
the database untouched. A few own their transaction and say so:
:func:`store_submission`, because it writes a submission and the obligation to
deliver it as one atomic unit and must roll both back together to settle an
idempotency race; :func:`claim_due_deliveries`, because a claim is only worth
anything once it is committed; :func:`complete_attempt`, because the audit
record and the state it justifies have to land together; and the two management
key writes, :func:`revoke_management_key` and :func:`record_management_key_use`,
because each is the whole of what its caller came to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hymical_forms import models
from hymical_forms.ingestion import Submission
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


def due_condition(now: datetime) -> ColumnElement[bool]:
    """
    build the test for a delivery a worker is allowed to pick up
    :param now: the instant to judge dueness against
    :returns: a SQL condition matching claimable deliveries
    """
    # Two ways to be claimable: waiting and due, or claimed by a worker whose
    # lease has run out. The second is what recovers work from a worker that died
    # holding a job, rather than leaving it stuck in ``processing`` forever.
    delivery = models.WebhookDelivery
    return or_(
        and_(delivery.state == DeliveryState.PENDING, delivery.next_attempt_at <= now),
        and_(delivery.state == DeliveryState.PROCESSING, delivery.claim_expires_at <= now),
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


def load_submissions(session: Session, submission_ids: list[str]) -> dict[str, Submission]:
    """
    load the submissions a batch of deliveries is carrying
    :param session: the session to query through
    :param submission_ids: the submissions to fetch
    :returns: the submissions in domain form, keyed by id
    """
    # One query for the batch rather than a lookup per delivery.
    rows = session.scalars(
        select(models.Submission).where(models.Submission.id.in_(submission_ids))
    )
    return {row.id: row.to_domain() for row in rows}


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
    attempt_number = delivery.attempts + 1
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
    delivery.claim_expires_at = None

    if result.outcome is DeliveryOutcome.SUCCEEDED:
        delivery.state = DeliveryState.DELIVERED
        delivery.completed_at = now
    elif is_retryable(result) and not policy.is_exhausted(attempt_number):
        delivery.state = DeliveryState.PENDING
        delivery.next_attempt_at = now + policy.delay_after(attempt_number)
    else:
        # Either the receiver said something repeating will not fix, or the
        # allowance ran out. Either way this is the last word on the delivery.
        delivery.state = DeliveryState.FAILED
        delivery.completed_at = now

    session.commit()
    return attempt


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
