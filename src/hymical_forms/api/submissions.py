"""
form ingestion endpoint: ``POST /f/{endpoint_id}``
"""

from __future__ import annotations

import math
from datetime import datetime
from http import HTTPStatus

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from python_multipart.exceptions import ParseError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

from hymical_forms import storage
from hymical_forms.config import Settings
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import (
    ENDPOINT_ID_RULE,
    Submission,
    build_submission,
    is_valid_endpoint_id,
)

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


@router.post(
    "/f/{endpoint_id}",
    status_code=HTTPStatus.ACCEPTED,
    summary="Submit a form",
    responses={
        400: {"model": ErrorResponse, "description": "Malformed form body"},
        404: {"model": ErrorResponse, "description": "Invalid or unknown endpoint ID"},
        409: {"model": ErrorResponse, "description": "Endpoint is not accepting submissions"},
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

    media_type = _media_type(request.headers.get("content-type"))
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise UnsupportedMediaType(media_type)

    settings: Settings = request.app.state.settings
    submission = build_submission(
        endpoint_id,
        await _parse_form(request, media_type, settings),
        max_fields=settings.max_fields,
        max_field_name_length=settings.max_field_name_length,
        max_field_value_length=settings.max_field_value_length,
    )

    await run_in_threadpool(_store, session, submission)

    return SubmissionAccepted(
        submission_id=submission.id,
        endpoint_id=submission.endpoint_id,
        received_at=submission.received_at,
        field_count=submission.field_count,
    )


def _store(session: Session, submission: Submission) -> None:
    """
    write the submission and make it durable
    :param session: the session to write through
    :param submission: the validated submission to store
    """
    # The commit happens inside the handler, not in the session dependency's
    # teardown, so that a failure still becomes an error response. Teardown runs
    # after the response has been sent, where raising could no longer change it.
    storage.add_submission(session, submission)
    session.commit()


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
