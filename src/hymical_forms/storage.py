"""
persistence operations, the only place queries are written

Callers own the transaction. Nothing here commits, so a request handler decides
when its work becomes durable and a failure anywhere before that commit leaves
the database untouched.
"""

from __future__ import annotations

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


def add_submission(session: Session, submission: Submission) -> models.Submission:
    """
    add an accepted submission
    :param session: the session to add the submission through
    :param submission: the validated domain submission to store
    :returns: the pending row, not yet committed
    """
    row = models.Submission.from_domain(submission)
    session.add(row)
    return row
