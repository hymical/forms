"""
endpoint management: creating, listing, inspecting and changing form endpoints

Every route here is behind the management authentication boundary, declared the
same way: by asking for :data:`~hymical_forms.api.security.ManagementKeyDep`.
None of them reads a submitted form's contents, and none of them reads a webhook
signing secret back out. A secret leaves this service only in the response of the
mutation that generated it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from http import HTTPStatus

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from hymical_forms import storage, webhooks
from hymical_forms.api.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorQuery,
    InvalidCursor,
    LimitQuery,
    next_cursor,
)
from hymical_forms.api.security import ManagementKeyDep
from hymical_forms.config import Settings
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import ENDPOINT_ID_RULE, is_valid_endpoint_id
from hymical_forms.models import ENDPOINT_NAME_MAX_LENGTH, Endpoint
from hymical_forms.webhooks import WEBHOOK_URL_MAX_LENGTH

logger = logging.getLogger(__name__)

router = APIRouter(tags=["endpoints"])

# Repeated on every management route in this module, so that an integrator meets
# one description of the authentication boundary rather than several.
UNAUTHENTICATED = {
    "model": ErrorResponse,
    "description": "Missing or invalid management API key",
}


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


class EndpointNotFound(ApiError):
    """
    raised when a management route addresses an endpoint that does not exist
    """

    # The same code the public ingestion route answers with for the same
    # condition, because it is the same condition: no endpoint holds that ID.
    status_code = HTTPStatus.NOT_FOUND
    code = "endpoint_not_found"

    def __init__(self, endpoint_id: str) -> None:
        """
        name the endpoint identifier that could not be resolved
        :param endpoint_id: the identifier taken from the request path
        """
        super().__init__(
            f"No form endpoint with the ID {endpoint_id!r} exists.",
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


class UpdateEndpointRequest(BaseModel):
    """
    the body accepted when changing an endpoint

    Every field is optional and an omitted one is left alone. ``webhook_url`` is
    the only field where an explicit ``null`` means something of its own: it
    removes the webhook. Sending ``null`` for the other two is the same as
    omitting them, because neither has a meaningful empty value.
    """

    # The endpoint's ID is deliberately not here. It is the primary key and it
    # appears in the action URL of every HTML form pointing at the endpoint, so
    # changing it would break deployed forms and orphan stored submissions.
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=ENDPOINT_NAME_MAX_LENGTH,
        description="New human-readable label. Omit to leave the label unchanged.",
    )
    is_active: bool | None = Field(
        default=None,
        description=(
            "Whether the endpoint accepts submissions. Setting it to false takes effect "
            "on the very next submission; queued deliveries are unaffected."
        ),
    )
    webhook_url: str | None = Field(
        default=None,
        max_length=WEBHOOK_URL_MAX_LENGTH,
        description=(
            "New http or https destination. Omit to leave the destination and its "
            "signing secret alone, or send null to remove the webhook entirely. A new "
            "destination is given a new signing secret, returned once in the response."
        ),
    )


class EndpointView(BaseModel):
    """
    an endpoint as returned by a management read
    """

    # No webhook_secret field exists here at all, rather than one that is always
    # null. A read route that cannot name the secret cannot leak it.
    id: str = Field(description="The public identifier the endpoint answers on.")
    name: str = Field(description="Human-readable label for the endpoint.")
    is_active: bool = Field(description="Whether the endpoint currently accepts submissions.")
    created_at: datetime = Field(description="UTC timestamp of when the endpoint was created.")
    webhook_url: str | None = Field(
        description=(
            "Where accepted submissions are delivered, or null if none is configured. "
            "This is configuration rather than a credential, so it is readable."
        )
    )


class EndpointPage(BaseModel):
    """
    one page of endpoints
    """

    items: list[EndpointView] = Field(description="The endpoints on this page, newest first.")
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` to read the next page, or null when this is certainly "
            "the last one. A full page always carries a cursor, so the final request "
            "of a walk returns an empty page."
        )
    )


class EndpointResponse(EndpointView):
    """
    an endpoint as returned by a mutation that may have generated a secret
    """

    webhook_secret: str | None = Field(
        description=(
            "The signing secret for this endpoint's webhook, returned only in the "
            "response of the request that generated it and never retrievable again. "
            "Null when this request did not generate one."
        )
    )


@router.post(
    "/endpoints",
    status_code=HTTPStatus.CREATED,
    summary="Create a form endpoint",
    responses={
        401: UNAUTHENTICATED,
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
    return _mutated(endpoint, secret)


@router.get(
    "/endpoints",
    summary="List form endpoints",
    responses={
        401: UNAUTHENTICATED,
        422: {"model": ErrorResponse, "description": "Invalid page size or cursor"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def list_endpoints(
    session: SessionDep,
    principal: ManagementKeyDep,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    cursor: CursorQuery = None,
) -> EndpointPage:
    """
    read a page of the endpoints this service holds
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :param limit: the most endpoints to return
    :param cursor: the previous page's cursor, or None to read the first page
    :returns: one page of endpoints, newest first
    :raises InvalidCursor: if the cursor does not continue from a known endpoint
    """
    try:
        page = storage.list_endpoints(session, limit=limit, after=cursor)
    except storage.UnknownCursor as exc:
        raise InvalidCursor() from exc

    return EndpointPage(
        items=[_view(endpoint) for endpoint in page],
        next_cursor=next_cursor([endpoint.id for endpoint in page], limit=limit),
    )


@router.get(
    "/endpoints/{endpoint_id}",
    summary="Inspect one form endpoint",
    responses={
        401: UNAUTHENTICATED,
        404: {"model": ErrorResponse, "description": "No such endpoint"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def get_endpoint(
    endpoint_id: str,
    session: SessionDep,
    principal: ManagementKeyDep,
) -> EndpointView:
    """
    read one endpoint's configuration
    :param endpoint_id: endpoint identifier taken from the request path
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :returns: the endpoint, carrying no signing secret
    :raises EndpointNotFound: if no endpoint holds that identifier
    """
    endpoint = storage.get_endpoint(session, endpoint_id)
    if endpoint is None:
        raise EndpointNotFound(endpoint_id)
    return _view(endpoint)


@router.patch(
    "/endpoints/{endpoint_id}",
    summary="Change a form endpoint",
    responses={
        401: UNAUTHENTICATED,
        404: {"model": ErrorResponse, "description": "No such endpoint"},
        422: {"model": ErrorResponse, "description": "Invalid name or webhook URL"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def update_endpoint(
    endpoint_id: str,
    payload: UpdateEndpointRequest,
    request: Request,
    session: SessionDep,
    principal: ManagementKeyDep,
) -> EndpointResponse:
    """
    change the parts of an endpoint's configuration that are safe to change
    :param endpoint_id: endpoint identifier taken from the request path
    :param payload: the fields to change, all of them optional
    :param request: the incoming request, read for the active configuration
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :returns: the endpoint as persisted, carrying a signing secret only if one was made
    :raises EndpointNotFound: if no endpoint holds that identifier
    """
    # PATCH rather than PUT, because only a few fields are mutable and a PUT would
    # promise that the body describes the whole resource. It does not: the ID and
    # the existing signing secret are not the caller's to send.
    endpoint = storage.get_endpoint_for_update(session, endpoint_id)
    if endpoint is None:
        raise EndpointNotFound(endpoint_id)

    settings: Settings = request.app.state.settings
    webhook_url = endpoint.webhook_url
    webhook_secret = endpoint.webhook_secret
    generated: str | None = None

    # ``model_fields_set`` is what separates "the caller sent null" from "the
    # caller said nothing", which for the destination are two different requests.
    if "webhook_url" in payload.model_fields_set and payload.webhook_url != webhook_url:
        webhook_url = payload.webhook_url
        if webhook_url is None:
            # Removing the webhook removes the secret with it, which the paired
            # check constraint requires and which is right anyway: a secret with
            # no destination is a credential nobody is using.
            webhook_secret = None
        else:
            try:
                webhooks.validate_webhook_url(
                    webhook_url,
                    allow_private_targets=settings.allow_private_webhook_targets,
                )
            except webhooks.WebhookUrlRejected as exc:
                raise InvalidWebhookUrl(exc.reason) from exc
            # A new destination gets a new secret, always. Carrying the old one
            # over would hand a receiver that never had it the ability to verify
            # signatures, and would leave the previous receiver holding a live
            # secret for payloads it no longer gets. An unchanged destination is
            # caught by the comparison above and keeps its secret untouched.
            webhook_secret = webhooks.new_signing_secret()
            generated = webhook_secret

    updated = storage.update_endpoint(
        session,
        endpoint,
        name=payload.name if payload.name is not None else endpoint.name,
        is_active=payload.is_active if payload.is_active is not None else endpoint.is_active,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
    )

    logger.info(
        "endpoint %s updated by management key %s (%s)",
        updated.id,
        principal.key_id,
        principal.name,
    )
    if generated is not None:
        # Says that a secret was made, never what it is.
        logger.info(
            "endpoint %s has a new webhook destination and a new signing secret", updated.id
        )

    # Deliveries already queued keep the destination and secret they snapshotted
    # when their submission was accepted. Changing configuration here does not
    # redirect work that is already owed, and does not leave a queued payload
    # signed with a secret its receiver never had.
    return _mutated(updated, generated)


def _view(endpoint: Endpoint) -> EndpointView:
    """
    render an endpoint for a management read
    :param endpoint: the persisted endpoint
    :returns: the endpoint's safe configuration
    """
    return EndpointView(
        id=endpoint.id,
        name=endpoint.name,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        webhook_url=endpoint.webhook_url,
    )


def _mutated(endpoint: Endpoint, secret: str | None) -> EndpointResponse:
    """
    render an endpoint for the response of a request that changed it
    :param endpoint: the persisted endpoint
    :param secret: a signing secret this request generated, or None if it made none
    :returns: the endpoint, carrying the new secret exactly once
    """
    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        webhook_url=endpoint.webhook_url,
        webhook_secret=secret,
    )
