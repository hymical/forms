"""
persistence operations, the only place queries are written

Most functions here leave the commit to the caller, so a request handler decides
when its work becomes durable and a failure anywhere before that commit leaves
the database untouched. :func:`store_submission` is the exception and owns its
transaction, because settling an idempotency race means rolling back a failed
insert and reading again, which cannot be split across a caller boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hymical_forms import models
from hymical_forms.ingestion import Submission


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
    session: Session, *, endpoint_id: str, name: str, is_active: bool
) -> models.Endpoint:
    """
    add an endpoint, failing if the identifier is taken
    :param session: the session to add the endpoint through
    :param endpoint_id: the public identifier the endpoint will answer on
    :param name: human-readable label for the endpoint
    :param is_active: whether the endpoint should accept submissions straight away
    :returns: the pending endpoint, not yet committed
    :raises EndpointAlreadyExists: if an endpoint already holds that identifier
    """
    endpoint = models.Endpoint(id=endpoint_id, name=name, is_active=is_active)
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
    idempotency_key: str | None = None,
    payload_fingerprint: str | None = None,
) -> StoredSubmission:
    """
    store a submission, or resolve it to the one an earlier attempt already stored
    :param session: the session to write through
    :param submission: the validated domain submission to store
    :param idempotency_key: the client's retry key, or None if it sent none
    :param payload_fingerprint: digest of the submitted content, required alongside a key
    :returns: the stored submission and whether it came from an earlier attempt
    :raises IdempotencyKeyReused: if the key was already used for different content
    """
    if idempotency_key is None:
        session.add(models.Submission.from_domain(submission))
        session.commit()
        return StoredSubmission(submission, replayed=False)

    # Fast path for the ordinary retry, where the first attempt already landed.
    existing = find_by_idempotency_key(session, submission.endpoint_id, idempotency_key)
    if existing is not None:
        return StoredSubmission(_settle(existing, payload_fingerprint), replayed=True)

    session.add(
        models.Submission.from_domain(
            submission,
            idempotency_key=idempotency_key,
            payload_fingerprint=payload_fingerprint,
        )
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
        # request's row, leaving exactly the one the winner committed.
        session.rollback()
        existing = find_by_idempotency_key(session, submission.endpoint_id, idempotency_key)
        if existing is None:
            # The violation was something other than the idempotency constraint,
            # so it is not ours to interpret and must not be reported as success.
            raise
        return StoredSubmission(_settle(existing, payload_fingerprint), replayed=True)

    return StoredSubmission(submission, replayed=False)


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
