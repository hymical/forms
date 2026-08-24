"""Form ingestion endpoint: ``POST /f/{endpoint_id}``."""

from __future__ import annotations

import math
from datetime import datetime
from http import HTTPStatus

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from python_multipart.exceptions import ParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

from hymical_forms.config import Settings
from hymical_forms.errors import ApiError, ErrorResponse
from hymical_forms.ingestion import (
    ENDPOINT_ID_MAX_LENGTH,
    ENDPOINT_ID_MIN_LENGTH,
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
    """The path segment is not a well-formed endpoint identifier."""

    status_code = HTTPStatus.NOT_FOUND
    code = "invalid_endpoint_id"

    def __init__(self) -> None:
        super().__init__(
            "The path does not address a form endpoint. Endpoint IDs are "
            f"{ENDPOINT_ID_MIN_LENGTH}-{ENDPOINT_ID_MAX_LENGTH} characters using lowercase "
            "letters, digits, '-' and '_', and must start and end with a letter or digit.",
        )


class UnsupportedMediaType(ApiError):
    """The request used a content type the ingestion endpoint cannot parse."""

    status_code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"

    def __init__(self, received: str) -> None:
        super().__init__(
            f"Form submissions must be sent as {URLENCODED} or {MULTIPART}.",
            details={
                "received": received[:_MEDIA_TYPE_ECHO_LIMIT] or None,
                "supported": list(SUPPORTED_MEDIA_TYPES),
            },
        )


class MalformedFormBody(ApiError):
    """The body did not parse as the declared form content type."""

    status_code = HTTPStatus.BAD_REQUEST
    code = "malformed_form_body"

    def __init__(self, reason: str) -> None:
        super().__init__(
            "The request body could not be parsed as form data.",
            details={"reason": reason},
        )


class FileUploadNotSupported(ApiError):
    """A multipart part carried a file, which this service does not accept."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "file_upload_not_supported"

    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"Field {field_name!r} carries a file upload, which is not supported.",
            details={"field": field_name},
        )


class SubmissionAccepted(BaseModel):
    """Acknowledgement returned for an accepted submission.

    The submitted values are not echoed back: the client already has them, and
    reflecting user input adds nothing but risk.
    """

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
        404: {"model": ErrorResponse, "description": "Invalid endpoint ID"},
        413: {"model": ErrorResponse, "description": "Request body too large"},
        415: {"model": ErrorResponse, "description": "Unsupported content type"},
        422: {"model": ErrorResponse, "description": "Submission rejected by an ingestion rule"},
    },
)
async def submit(endpoint_id: str, request: Request) -> SubmissionAccepted:
    """Accept an HTML form submission.

    The response is ``202 Accepted`` rather than ``201 Created``: the submission
    is acknowledged as received and well-formed, but Hymical Forms does not yet
    persist it or deliver it anywhere.
    """
    if not is_valid_endpoint_id(endpoint_id):
        raise InvalidEndpointId()

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

    return SubmissionAccepted(
        submission_id=submission.id,
        endpoint_id=submission.endpoint_id,
        received_at=submission.received_at,
        field_count=submission.field_count,
    )


async def _parse_form(
    request: Request, media_type: str, settings: Settings
) -> list[tuple[str, str]]:
    """Parse the body into ordered name/value pairs, preserving repeated names.

    The parser is selected from the media type we normalized ourselves rather
    than through ``Request.form()``, whose dispatch compares the header verbatim
    even though media types are case-insensitive (RFC 9110 §8.3).

    Starlette's own field and part limits are disabled: the request body size cap
    already bounds memory use, and leaving them on would let a library-defined
    threshold shadow the limits configured for this service.
    """
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
    return exc.message if isinstance(exc, MultiPartException) else str(exc)


def _media_type(content_type: str | None) -> str:
    """Strip parameters such as ``charset`` and ``boundary`` from a Content-Type."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()
