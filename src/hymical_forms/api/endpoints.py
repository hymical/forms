"""
endpoint management: ``POST /endpoints``
"""

from __future__ import annotations

import logging
from datetime import datetime
from http import HTTPStatus

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from hymical_forms import storage, webhooks
from hymical_forms.api.security import ManagementKeyDep
from hymical_forms.config import Settings
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import ENDPOINT_ID_RULE, is_valid_endpoint_id
from hymical_forms.models import ENDPOINT_NAME_MAX_LENGTH
from hymical_forms.webhooks import WEBHOOK_URL_MAX_LENGTH

logger = logging.getLogger(__name__)

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


class InvalidWebhookUrl(ApiError):
    """
    raised when a webhook destination is malformed or not permitted
    """

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "invalid_webhook_url"

    def __init__(self, reason: str) -> None:
        """
        report why the destination was refused
        :param reason: short phrase completing "the webhook URL ..."
        """
        super().__init__(f"The webhook URL {reason}.", details={"field": "webhook_url"})


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
    webhook_url: str | None = Field(
        default=None,
        max_length=WEBHOOK_URL_MAX_LENGTH,
        description=(
            "Optional http or https destination to deliver accepted submissions to. "
            "A signing secret is generated for it and returned once."
        ),
    )


class EndpointResponse(BaseModel):
    """
    an endpoint as returned by the API
    """

    id: str = Field(description="The public identifier the endpoint answers on.")
    name: str = Field(description="Human-readable label for the endpoint.")
    is_active: bool = Field(description="Whether the endpoint currently accepts submissions.")
    created_at: datetime = Field(description="UTC timestamp of when the endpoint was created.")
    webhook_url: str | None = Field(
        description="Where accepted submissions are delivered, or null if none is configured."
    )
    webhook_secret: str | None = Field(
        description=(
            "The signing secret for this endpoint's webhook. Returned only here, at "
            "creation, and never retrievable again. Null if no webhook is configured."
        )
    )


@router.post(
    "/endpoints",
    status_code=HTTPStatus.CREATED,
    summary="Create a form endpoint",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid management API key"},
        409: {"model": ErrorResponse, "description": "Endpoint ID already taken"},
        422: {
            "model": ErrorResponse,
            "description": "Invalid endpoint ID, name, or webhook URL",
        },
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def create_endpoint(
    payload: CreateEndpointRequest,
    request: Request,
    session: SessionDep,
    principal: ManagementKeyDep,
) -> EndpointResponse:
    """
    create an endpoint that submissions may then be addressed to
    :param payload: the endpoint identifier, label, active state and optional webhook
    :param request: the incoming request, read for the active configuration
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :returns: the endpoint as persisted, including its signing secret if one was made
    """
    # A plain ``def`` route, so FastAPI runs it in a worker thread and the
    # synchronous database calls never block the event loop.
    #
    # The authenticated key is not recorded on the endpoint. A management key
    # administers the service rather than owning a slice of it, and an
    # ``api_key_id`` column here would be the beginning of a tenancy model
    # nothing in this build has a use for.
    if not is_valid_endpoint_id(payload.id):
        raise InvalidEndpointId()

    settings: Settings = request.app.state.settings
    secret: str | None = None
    if payload.webhook_url is not None:
        try:
            webhooks.validate_webhook_url(
                payload.webhook_url,
                allow_private_targets=settings.allow_private_webhook_targets,
            )
        except webhooks.WebhookUrlRejected as exc:
            raise InvalidWebhookUrl(exc.reason) from exc
        # Generated here rather than accepted from the caller, so its strength is
        # this service's responsibility and never an integrator's oversight.
        secret = webhooks.new_signing_secret()

    try:
        endpoint = storage.create_endpoint(
            session,
            endpoint_id=payload.id,
            name=payload.name,
            is_active=payload.is_active,
            webhook_url=payload.webhook_url,
            webhook_secret=secret,
        )
    except storage.EndpointAlreadyExists as exc:
        raise EndpointIdConflict(payload.id) from exc

    session.commit()

    # Configuring a webhook destination is the most consequential thing this API
    # does, so which credential did it is worth having in the log. Only the key's
    # non-secret identity is available to log, which is the point of the
    # principal carrying nothing else.
    logger.info(
        "endpoint %s created by management key %s (%s)",
        endpoint.id,
        principal.key_id,
        principal.name,
    )

    # The secret leaves the service exactly once, in this response. There is no
    # route that reads it back, so a caller that loses it has to make a new
    # endpoint rather than being handed the old secret again.
    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        webhook_url=endpoint.webhook_url,
        webhook_secret=secret,
    )
