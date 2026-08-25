"""
delivery inspection and manual replay: the operational view of the outbox

These routes read the durable delivery queue and can put one terminally failed
delivery back into it. They never send anything: the API process holds no
outbound HTTP client, and replay is a state change that makes existing work
claimable by the existing worker again.

Nothing here exposes the signing secret a delivery snapshotted, and nothing here
exposes the submitted form contents the delivery carries.
"""

from __future__ import annotations

import logging
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from hymical_forms import models, storage
from hymical_forms.api.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorQuery,
    InvalidCursor,
    LimitQuery,
    next_cursor,
)
from hymical_forms.api.security import ManagementKeyDep
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import ENDPOINT_ID_MAX_LENGTH
from hymical_forms.models import utcnow
from hymical_forms.webhooks import DeliveryOutcome, DeliveryState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deliveries"])

UNAUTHENTICATED = {
    "model": ErrorResponse,
    "description": "Missing or invalid management API key",
}

EndpointFilter = Annotated[
    str | None,
    Query(
        max_length=ENDPOINT_ID_MAX_LENGTH,
        description="Only deliveries for this endpoint. Omit for every endpoint.",
    ),
]

StateFilter = Annotated[
    DeliveryState | None,
    Query(description="Only deliveries in this state. Omit for every state."),
]


class DeliveryNotFound(ApiError):
    """
    raised when a management route addresses a delivery that does not exist
    """

    status_code = HTTPStatus.NOT_FOUND
    code = "delivery_not_found"

    def __init__(self, delivery_id: str) -> None:
        """
        name the delivery identifier that could not be resolved
        :param delivery_id: the identifier taken from the request path
        """
        super().__init__(
            f"No webhook delivery with the ID {delivery_id!r} exists.",
            details={"delivery_id": delivery_id},
        )


class DeliveryNotReplayable(ApiError):
    """
    raised when a replay was asked for a delivery that has not terminally failed
    """

    # A 409 rather than a 422: the body was fine and the request is refused
    # because of the state the resource is in, which is what 409 describes. It is
    # deliberately not a 500, because this is an ordinary answer rather than a
    # fault.
    status_code = HTTPStatus.CONFLICT
    code = "delivery_not_replayable"

    def __init__(self, delivery_id: str, state: str) -> None:
        """
        report the state that makes the delivery ineligible for replay
        :param delivery_id: the delivery the caller asked to replay
        :param state: the state the delivery is actually in
        """
        # The state is named because it is what tells an operator whether to wait,
        # to stop, or that somebody else replayed this a moment ago.
        super().__init__(
            f"Only a delivery that has terminally failed can be replayed. Delivery "
            f"{delivery_id!r} is {state!r}.",
            details={"delivery_id": delivery_id, "state": state},
        )


class DeliveryAttemptView(BaseModel):
    """
    one recorded outbound request, as returned by the API
    """

    # Response bodies are not stored by this service and so cannot be reported
    # here. Neither the signing secret nor any request header is stored either.
    attempt_number: int = Field(
        description=(
            "Position of this attempt in the delivery's lifetime history, starting at 1. "
            "Numbers are never reused, including across a manual replay."
        )
    )
    attempted_at: datetime = Field(description="UTC timestamp of when the request was made.")
    outcome: DeliveryOutcome = Field(description="What the attempt produced.")
    response_status: int | None = Field(
        description="HTTP status the destination answered with, or null if it never answered."
    )
    error: str | None = Field(
        description="Bounded failure message, or null when the attempt succeeded."
    )


class DeliveryView(BaseModel):
    """
    one logical delivery, as returned by the API
    """

    id: str = Field(description="Opaque identifier for this logical delivery.")
    submission_id: str | None = Field(
        description=(
            "The submission this delivery carries, or null once retention has removed "
            "it. Only a delivery that has already been delivered can lose its "
            "submission: every state a delivery can still be attempted from keeps the "
            "payload it would need."
        )
    )
    endpoint_id: str = Field(
        description=(
            "The endpoint the submission was addressed to, recorded on the delivery "
            "when it was queued so that it survives the submission being removed."
        )
    )
    state: DeliveryState = Field(description="Where this delivery has got to.")
    destination_url: str = Field(
        description=(
            "Where this delivery is sent, as snapshotted when the submission was "
            "accepted. Changing the endpoint's webhook does not change it."
        )
    )
    attempt_count: int = Field(
        description="How many requests have ever been made for this delivery."
    )
    cycle_attempt_count: int = Field(
        description=(
            "How many requests have been made since the delivery last entered the queue. "
            "The retry allowance is measured against this, and a manual replay resets it."
        )
    )
    next_attempt_at: datetime = Field(
        description="UTC timestamp of when this delivery next becomes due."
    )
    created_at: datetime = Field(description="UTC timestamp of when the delivery was queued.")
    completed_at: datetime | None = Field(
        description="UTC timestamp of when the delivery finished, or null if it has not."
    )


class DeliveryDetail(DeliveryView):
    """
    one logical delivery together with its ordered attempt history
    """

    attempts: list[DeliveryAttemptView] = Field(
        description="Every request made for this delivery, lowest attempt number first."
    )


class DeliveryPage(BaseModel):
    """
    one page of deliveries
    """

    items: list[DeliveryView] = Field(description="The deliveries on this page, newest first.")
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` to read the next page, or null when this is certainly "
            "the last one. A full page always carries a cursor, so the final request "
            "of a walk returns an empty page."
        )
    )


@router.get(
    "/deliveries",
    summary="List webhook deliveries",
    responses={
        401: UNAUTHENTICATED,
        422: {"model": ErrorResponse, "description": "Invalid filter, page size or cursor"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def list_deliveries(
    session: SessionDep,
    principal: ManagementKeyDep,
    endpoint_id: EndpointFilter = None,
    state: StateFilter = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    cursor: CursorQuery = None,
) -> DeliveryPage:
    """
    read a page of the delivery queue
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :param endpoint_id: only deliveries for this endpoint, or None for every endpoint
    :param state: only deliveries in this state, or None for every state
    :param limit: the most deliveries to return
    :param cursor: the previous page's cursor, or None to read the first page
    :returns: one page of deliveries, newest first
    :raises InvalidCursor: if the cursor does not continue from a known delivery
    """
    # The submitted fields are not summarised, counted or echoed here. Reading a
    # submission back is not something this API does yet, and a delivery listing
    # is not the place to start.
    try:
        page = storage.list_deliveries(
            session,
            limit=limit,
            after=cursor,
            endpoint_id=endpoint_id,
            state=state.value if state is not None else None,
        )
    except storage.UnknownCursor as exc:
        raise InvalidCursor() from exc

    return DeliveryPage(
        items=[_view(delivery) for delivery in page],
        next_cursor=next_cursor([delivery.id for delivery in page], limit=limit),
    )


@router.get(
    "/deliveries/{delivery_id}",
    summary="Inspect one webhook delivery",
    responses={
        401: UNAUTHENTICATED,
        404: {"model": ErrorResponse, "description": "No such delivery"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def get_delivery(
    delivery_id: str,
    session: SessionDep,
    principal: ManagementKeyDep,
) -> DeliveryDetail:
    """
    read one delivery and the ordered history of the requests made for it
    :param delivery_id: delivery identifier taken from the request path
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :returns: the delivery and its attempts, carrying no signing secret
    :raises DeliveryNotFound: if no delivery holds that identifier
    """
    delivery = storage.get_delivery(session, delivery_id)
    if delivery is None:
        raise DeliveryNotFound(delivery_id)

    attempts = storage.list_delivery_attempts(session, delivery_id)
    return DeliveryDetail(
        **_view(delivery).model_dump(),
        attempts=[_attempt(attempt) for attempt in attempts],
    )


@router.post(
    "/deliveries/{delivery_id}/replay",
    summary="Requeue a failed webhook delivery",
    responses={
        401: UNAUTHENTICATED,
        404: {"model": ErrorResponse, "description": "No such delivery"},
        409: {"model": ErrorResponse, "description": "Delivery has not terminally failed"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def replay_delivery(
    delivery_id: str,
    session: SessionDep,
    principal: ManagementKeyDep,
) -> DeliveryView:
    """
    put a terminally failed delivery back into the queue for the worker
    :param delivery_id: delivery identifier taken from the request path
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :returns: the delivery as it now stands, due and waiting for a worker
    :raises DeliveryNotFound: if no delivery holds that identifier
    :raises DeliveryNotReplayable: if the delivery has not terminally failed
    """
    # 200 rather than 202. A 202 would say "your request has been accepted and
    # will be carried out later", which invites reading the replay itself as the
    # delivery. It is not: this request completes here, and what it leaves behind
    # is a row the ordinary worker will claim on its next poll. The body is the
    # delivery's new state, which is the whole of what this request did.
    #
    # No outbound request is made. This process has no HTTP client to make one
    # with, which is what keeps that guarantee structural rather than a promise.
    outcome = storage.requeue_failed_delivery(session, delivery_id, now=utcnow())
    if outcome.record is None:
        raise DeliveryNotFound(delivery_id)
    if not outcome.requeued:
        # Either the delivery was never failed, or two operators replayed it at
        # once and this is the loser. Both are answered from the state the
        # database settled on, so the answer is the same however the race went.
        raise DeliveryNotReplayable(delivery_id, outcome.record.state)

    logger.info(
        "delivery %s requeued by management key %s (%s)",
        delivery_id,
        principal.key_id,
        principal.name,
    )
    return _view(outcome.record)


def _view(delivery: models.WebhookDelivery) -> DeliveryView:
    """
    render a delivery for a management read
    :param delivery: the persisted delivery
    :returns: the delivery's operational state, carrying no signing secret
    """
    return DeliveryView(
        id=delivery.id,
        submission_id=delivery.submission_id,
        endpoint_id=delivery.endpoint_id,
        state=DeliveryState(delivery.state),
        destination_url=delivery.destination_url,
        attempt_count=delivery.attempts,
        cycle_attempt_count=delivery.cycle_attempts,
        next_attempt_at=delivery.next_attempt_at,
        created_at=delivery.created_at,
        completed_at=delivery.completed_at,
    )


def _attempt(attempt: models.DeliveryAttempt) -> DeliveryAttemptView:
    """
    render one recorded attempt for a management read
    :param attempt: the persisted attempt
    :returns: what the attempt produced, with no credential material
    """
    return DeliveryAttemptView(
        attempt_number=attempt.attempt_number,
        attempted_at=attempt.attempted_at,
        outcome=DeliveryOutcome(attempt.outcome),
        response_status=attempt.response_status,
        error=attempt.error,
    )
