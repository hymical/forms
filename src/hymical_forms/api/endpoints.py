"""
endpoint management: ``POST /endpoints``
"""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus

from fastapi import APIRouter
from pydantic import BaseModel, Field

from hymical_forms import storage
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import ENDPOINT_ID_RULE, is_valid_endpoint_id
from hymical_forms.models import ENDPOINT_NAME_MAX_LENGTH

router = APIRouter(tags=["endpoints"])


class InvalidEndpointId(ApiError):
    """
    raised when a submitted endpoint identifier does not follow the rules
    """

    # The ingestion route answers 404 for a malformed ID because the path simply
    # does not address an endpoint. Here the ID arrives in a request body, where
    # the same problem is an unprocessable field rather than a missing resource.
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "invalid_endpoint_id"

    def __init__(self) -> None:
        """
        state the endpoint identifier rules the request failed
        """
        super().__init__(ENDPOINT_ID_RULE, details={"field": "id"})


class EndpointIdConflict(ApiError):
    """
    raised when the requested endpoint identifier is already taken
    """

    status_code = HTTPStatus.CONFLICT
    code = "endpoint_already_exists"

    def __init__(self, endpoint_id: str) -> None:
        """
        name the endpoint identifier that is already in use
        :param endpoint_id: the identifier that collided
        """
        super().__init__(
            f"An endpoint with the ID {endpoint_id!r} already exists.",
            details={"endpoint_id": endpoint_id},
        )


class CreateEndpointRequest(BaseModel):
    """
    the body accepted when creating an endpoint
    """

    id: str = Field(description=ENDPOINT_ID_RULE)
    name: str = Field(
        min_length=1,
        max_length=ENDPOINT_NAME_MAX_LENGTH,
        description="Human-readable label, shown to whoever administers the endpoint.",
    )
    is_active: bool = Field(
        default=True,
        description="Whether the endpoint accepts submissions. Inactive endpoints reject them.",
    )


class EndpointResponse(BaseModel):
    """
    an endpoint as returned by the API
    """

    id: str = Field(description="The public identifier the endpoint answers on.")
    name: str = Field(description="Human-readable label for the endpoint.")
    is_active: bool = Field(description="Whether the endpoint currently accepts submissions.")
    created_at: datetime = Field(description="UTC timestamp of when the endpoint was created.")


@router.post(
    "/endpoints",
    status_code=HTTPStatus.CREATED,
    summary="Create a form endpoint",
    responses={
        409: {"model": ErrorResponse, "description": "Endpoint ID already taken"},
        422: {"model": ErrorResponse, "description": "Invalid endpoint ID or name"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def create_endpoint(payload: CreateEndpointRequest, session: SessionDep) -> EndpointResponse:
    """
    create an endpoint that submissions may then be addressed to
    :param payload: the endpoint identifier, label, and initial active state
    :param session: the session this request does its database work through
    :returns: the endpoint as persisted
    """
    # A plain ``def`` route, so FastAPI runs it in a worker thread and the
    # synchronous database calls never block the event loop.
    if not is_valid_endpoint_id(payload.id):
        raise InvalidEndpointId()

    try:
        endpoint = storage.create_endpoint(
            session,
            endpoint_id=payload.id,
            name=payload.name,
            is_active=payload.is_active,
        )
    except storage.EndpointAlreadyExists as exc:
        raise EndpointIdConflict(payload.id) from exc

    session.commit()

    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
    )
