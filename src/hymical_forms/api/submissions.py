"""
form ingestion endpoint: ``POST /f/{endpoint_id}``
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from http import HTTPStatus

import httpx2
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from python_multipart.exceptions import ParseError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

from hymical_forms import storage, webhooks
from hymical_forms.config import Settings
from hymical_forms.db import SessionDep
from hymical_forms.delivery import deliver
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import (
    ENDPOINT_ID_RULE,
    IDEMPOTENCY_KEY_RULE,
    build_submission,
    is_valid_endpoint_id,
    is_valid_idempotency_key,
    payload_fingerprint,
)
from hymical_forms.webhooks import DeliveryOutcome, DeliveryResult

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

URLENCODED = "application/x-www-form-urlencoded"
MULTIPART = "multipart/form-data"
SUPPORTED_MEDIA_TYPES = (URLENCODED, MULTIPART)

# Content-Type values are echoed back to help developers debug their form tags,
# but only ever a bounded prefix of what the client sent.
_MEDIA_TYPE_ECHO_LIMIT = 128

router = APIRouter(tags=["submissions"])


class InvalidEndpointId(ApiError):
    """
    raised when the path segment is not a well-formed endpoint identifier
    """

    status_code = HTTPStatus.NOT_FOUND
    code = "invalid_endpoint_id"

    def __init__(self) -> None:
        """
        state the endpoint identifier rules the request failed
        """
        super().__init__(f"The path does not address a form endpoint. {ENDPOINT_ID_RULE}")


class EndpointNotFound(ApiError):
    """
    raised when the identifier is well formed but no such endpoint exists
    """

    # Deliberately the same status as a malformed ID: from outside, both mean the
    # path does not address a form endpoint. The code tells the two apart.
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


class EndpointInactive(ApiError):
    """
    raised when the endpoint exists but is not accepting submissions
    """

    status_code = HTTPStatus.CONFLICT
    code = "endpoint_inactive"

    def __init__(self, endpoint_id: str) -> None:
        """
        name the endpoint identifier that is not accepting submissions
        :param endpoint_id: the identifier taken from the request path
        """
        super().__init__(
            f"The form endpoint {endpoint_id!r} is not accepting submissions.",
            details={"endpoint_id": endpoint_id},
        )


class InvalidIdempotencyKey(ApiError):
    """
    raised when the ``Idempotency-Key`` header is present but unusable
    """

    # A malformed header is a framing problem rather than a semantic one, which
    # is what separates this from the 422 an unacceptable submission earns.
    status_code = HTTPStatus.BAD_REQUEST
    code = "invalid_idempotency_key"

    def __init__(self) -> None:
        """
        state the idempotency key rules the request failed
        """
        super().__init__(
            f"The {IDEMPOTENCY_KEY_HEADER} header is not usable. {IDEMPOTENCY_KEY_RULE}"
        )


class IdempotencyConflict(ApiError):
    """
    raised when an idempotency key was already spent on different content
    """

    status_code = HTTPStatus.CONFLICT
    code = "idempotency_conflict"

    def __init__(self, endpoint_id: str, idempotency_key: str) -> None:
        """
        report that the key is already tied to a different submission
        :param endpoint_id: the endpoint the key is scoped to
        :param idempotency_key: the key the client reused
        """
        # The earlier submission's content is never described, only the fact that
        # it differs, so the key cannot be used to read back someone else's form.
        super().__init__(
            f"The {IDEMPOTENCY_KEY_HEADER} {idempotency_key!r} was already used on endpoint "
            f"{endpoint_id!r} for a different submission.",
            details={"endpoint_id": endpoint_id, "idempotency_key": idempotency_key},
        )


class UnsupportedMediaType(ApiError):
    """
    raised when the request used a content type the endpoint cannot parse
    """

    status_code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"

    def __init__(self, received: str) -> None:
        """
        report the rejected content type alongside the supported ones
        :param received: the normalized media type taken from the request
        """
        super().__init__(
            f"Form submissions must be sent as {URLENCODED} or {MULTIPART}.",
            details={
                "received": received[:_MEDIA_TYPE_ECHO_LIMIT] or None,
                "supported": list(SUPPORTED_MEDIA_TYPES),
            },
        )


class MalformedFormBody(ApiError):
    """
    raised when the body did not parse as the declared form content type
    """

    status_code = HTTPStatus.BAD_REQUEST
    code = "malformed_form_body"

    def __init__(self, reason: str) -> None:
        """
        report why the body could not be parsed
        :param reason: the form parser's description of what went wrong
        """
        super().__init__(
            "The request body could not be parsed as form data.",
            details={"reason": reason},
        )


class FileUploadNotSupported(ApiError):
    """
    raised when a multipart part carries a file, which this service does not accept
    """

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "file_upload_not_supported"

    def __init__(self, field_name: str) -> None:
        """
        name the field that carried a file part
        :param field_name: name of the offending multipart field
        """
        super().__init__(
            f"Field {field_name!r} carries a file upload, which is not supported.",
            details={"field": field_name},
        )


class DeliveryStatus(BaseModel):
    """
    what happened to the webhook for this submission, if anything
    """

    attempted: bool = Field(
        description=(
            "Whether a webhook delivery was attempted for this request. False when the "
            "endpoint has no webhook, and false on an idempotent replay, which never "
            "redelivers."
        )
    )
    outcome: DeliveryOutcome | None = Field(
        description="Result of the attempt, or null when none was made."
    )


class SubmissionAccepted(BaseModel):
    """
    acknowledgement returned for an accepted submission
    """

    # The submitted values are not echoed back: the client already has them, and
    # reflecting user input adds nothing but risk.
    submission_id: str = Field(description="Opaque identifier generated for this submission.")
    endpoint_id: str = Field(description="The endpoint the submission was addressed to.")
    received_at: datetime = Field(description="UTC timestamp of when the API accepted the body.")
    field_count: int = Field(description="Number of name/value pairs the submission carried.")
    idempotent_replay: bool = Field(
        description=(
            "True when this response describes a submission an earlier request already "
            "stored, rather than one created now. Always false without an "
            "Idempotency-Key header."
        ),
    )
    delivery: DeliveryStatus = Field(
        description="What happened to this endpoint's webhook, if it has one."
    )


@router.post(
    "/f/{endpoint_id}",
    status_code=HTTPStatus.ACCEPTED,
    summary="Submit a form",
    responses={
        400: {"model": ErrorResponse, "description": "Malformed form body or idempotency key"},
        404: {"model": ErrorResponse, "description": "Invalid or unknown endpoint ID"},
        409: {
            "model": ErrorResponse,
            "description": "Endpoint is not accepting submissions, or idempotency key reused",
        },
        413: {"model": ErrorResponse, "description": "Request body too large"},
        415: {"model": ErrorResponse, "description": "Unsupported content type"},
        422: {"model": ErrorResponse, "description": "Submission rejected by an ingestion rule"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
async def submit(endpoint_id: str, request: Request, session: SessionDep) -> SubmissionAccepted:
    """
    accept an html form submission and store it
    :param endpoint_id: endpoint identifier taken from the request path
    :param request: the incoming request, read for its content type and body
    :param session: the session this request does its database work through
    :returns: an acknowledgement carrying the stored submission's metadata
    """
    # The response is 202 Accepted rather than 201 Created: the submission is
    # stored, but the delivery it was accepted for has not happened yet.
    #
    # The endpoint is resolved before the body is parsed, so an unknown endpoint
    # costs one indexed lookup rather than a full parse of a body we would throw
    # away. This handler must stay ``async`` to stream the body, so each blocking
    # database call is handed to a worker thread instead of stalling the loop.
    if not is_valid_endpoint_id(endpoint_id):
        raise InvalidEndpointId()

    endpoint = await run_in_threadpool(storage.get_endpoint, session, endpoint_id)
    if endpoint is None:
        raise EndpointNotFound(endpoint_id)
    if not endpoint.is_active:
        raise EndpointInactive(endpoint_id)

    # Read the webhook configuration off the row now, while the session is known
    # to be clean. Storing the submission can roll back to settle an idempotency
    # race, and a rollback expires loaded objects, so touching the endpoint later
    # would silently issue a refresh query from this async handler.
    webhook_url = endpoint.webhook_url
    webhook_secret = endpoint.webhook_secret

    media_type = _media_type(request.headers.get("content-type"))
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise UnsupportedMediaType(media_type)

    idempotency_key = _idempotency_key(request)

    settings: Settings = request.app.state.settings
    submission = build_submission(
        endpoint_id,
        await _parse_form(request, media_type, settings),
        max_fields=settings.max_fields,
        max_field_name_length=settings.max_field_name_length,
        max_field_value_length=settings.max_field_value_length,
    )

    # The commit happens inside the handler, not in the session dependency's
    # teardown, so that a failure still becomes an error response. Teardown runs
    # after the response has been sent, where raising could no longer change it.
    try:
        stored = await run_in_threadpool(
            storage.store_submission,
            session,
            submission,
            idempotency_key=idempotency_key,
            payload_fingerprint=(
                payload_fingerprint(submission.fields) if idempotency_key else None
            ),
        )
    except storage.IdempotencyKeyReused as exc:
        raise IdempotencyConflict(exc.endpoint_id, exc.idempotency_key) from exc

    # The submission is durable from here on. Everything below is downstream
    # delivery, and none of it may turn an accepted submission into a failure.
    delivery = await _deliver(request, session, stored, webhook_url, webhook_secret)

    # A replay answers with the original submission's identity and timestamp, so
    # a client that retried after a lost response ends up describing one event.
    return SubmissionAccepted(
        submission_id=stored.submission.id,
        endpoint_id=stored.submission.endpoint_id,
        received_at=stored.submission.received_at,
        field_count=stored.submission.field_count,
        idempotent_replay=stored.replayed,
        delivery=delivery,
    )


async def _deliver(
    request: Request,
    session: Session,
    stored: storage.StoredSubmission,
    webhook_url: str | None,
    webhook_secret: str | None,
) -> DeliveryStatus:
    """
    make the one delivery attempt this submission is owed, if it is owed one
    :param request: the incoming request, read for the shared outbound client
    :param session: the session to record the attempt through
    :param stored: the submission as it was stored, and whether it was a replay
    :param webhook_url: the endpoint's destination, or None if it has no webhook
    :param webhook_secret: the destination's signing secret
    :returns: what the caller should be told about delivery
    """
    # A replay is a client retrying a request whose submission already exists,
    # and that submission already had its attempt. Delivering again would turn a
    # lost response into duplicate downstream processing, which is the exact
    # problem the idempotency key was introduced to solve.
    if webhook_url is None or webhook_secret is None or stored.replayed:
        return DeliveryStatus(attempted=False, outcome=None)

    client: httpx2.AsyncClient = request.app.state.webhook_client
    body = webhooks.serialize_payload(webhooks.build_payload(stored.submission))
    result = await deliver(client, url=webhook_url, secret=webhook_secret, body=body)

    await run_in_threadpool(_record_attempt, session, stored.submission.id, webhook_url, result)
    return DeliveryStatus(attempted=True, outcome=result.outcome)


def _record_attempt(
    session: Session, submission_id: str, destination_url: str, result: DeliveryResult
) -> None:
    """
    write the delivery attempt, without letting a bookkeeping failure escape
    :param session: the session to write through
    :param submission_id: the submission the attempt was delivering
    :param destination_url: the URL the attempt was sent to
    :param result: the outcome of the attempt
    """
    try:
        storage.record_delivery_attempt(
            session,
            submission_id=submission_id,
            destination_url=destination_url,
            result=result,
        )
    except SQLAlchemyError:
        # By now the submission is durable and the webhook has already been sent.
        # Answering with an error would tell the client its form was lost, which
        # is untrue, and would invite a retry that delivers a second time. Losing
        # the record costs observability, not correctness, so it is logged for an
        # operator and the request still succeeds.
        session.rollback()
        logger.exception("could not record webhook delivery attempt for %s", submission_id)


def _idempotency_key(request: Request) -> str | None:
    """
    read and validate the retry key a client may have sent
    :param request: the incoming request
    :returns: the key, or None if the header was absent
    :raises InvalidIdempotencyKey: if the header is present but breaks the key rules
    """
    # An absent header keeps the pre-idempotency behaviour exactly: every accepted
    # request stores a new submission. A header that is present but empty is a
    # client bug, and treating it as absent would silently drop the guarantee the
    # client was asking for.
    key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if key is None:
        return None
    if not is_valid_idempotency_key(key):
        raise InvalidIdempotencyKey()
    return key


async def _parse_form(
    request: Request, media_type: str, settings: Settings
) -> list[tuple[str, str]]:
    """
    parse the body into ordered name/value pairs, preserving repeated names
    :param request: the incoming request, streamed into the form parser
    :param media_type: normalized media type taken from the Content-Type header
    :param settings: active configuration, used to size the parser buffers
    :returns: ordered name/value pairs exactly as submitted
    :raises MalformedFormBody: if the body does not parse as the declared media type
    :raises FileUploadNotSupported: if a multipart part carries a file
    """
    # The parser is selected from the media type we normalized ourselves rather
    # than through ``Request.form()``, whose dispatch compares the header verbatim
    # even though media types are case-insensitive (RFC 9110 section 8.3).
    #
    # Starlette's own field and part limits are disabled: the request body size cap
    # already bounds memory use, and leaving them on would let a library-defined
    # threshold shadow the limits configured for this service.
    parser: FormParser | MultiPartParser
    if media_type == MULTIPART:
        parser = MultiPartParser(
            request.headers,
            request.stream(),
            max_files=math.inf,
            max_fields=math.inf,
            max_part_size=settings.max_body_bytes,
        )
    else:
        parser = FormParser(
            request.headers,
            request.stream(),
            max_fields=math.inf,
            max_part_size=settings.max_body_bytes,
        )

    try:
        form = await parser.parse()
    except (MultiPartException, ParseError) as exc:
        raise MalformedFormBody(_failure_reason(exc)) from exc

    try:
        items: list[tuple[str, str]] = []
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                raise FileUploadNotSupported(name)
            items.append((name, value))
        return items
    finally:
        await form.close()


def _failure_reason(exc: MultiPartException | ParseError) -> str:
    """
    extract a human-readable reason from a form parser failure
    :param exc: the exception raised while parsing the body
    :returns: the parser's description of what went wrong
    """
    return exc.message if isinstance(exc, MultiPartException) else str(exc)


def _media_type(content_type: str | None) -> str:
    """
    strip parameters such as charset and boundary from a Content-Type header
    :param content_type: raw header value, or None when the header is absent
    :returns: the lowercased media type, or an empty string when there is none
    """
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()
