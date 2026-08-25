"""
submission retrieval and export: the first supported way to read a form back

Every route here is behind the management authentication boundary, declared the
same way as the rest of the management API, by asking for
:data:`~hymical_forms.api.security.ManagementKeyDep`. Nothing here is reachable
without a management API key, and the public ingestion route deliberately still
answers with an acknowledgement rather than with anything a form carried.

These are the only routes in the service that return submitted values. That makes
the boundary worth stating rather than assuming:

* a listing is metadata only. It reports how many values a submission carried,
  never what they were, so paging through a busy endpoint does not spread form
  content across pages nobody asked for;
* the detail route and the exports return the values themselves, in the shape
  they are stored, because reading one submission back is the whole point of
  asking for it;
* nothing here logs a field name or a field value. What is logged about an export
  is who asked, for what filter, and how many rows it came to.

The payload fingerprint and the idempotency key are never returned. The
fingerprint is an internal detail of how a retry is recognised, and the key is a
secret in practice: anyone holding it can resolve it to a submission through the
public ingestion route.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response, StreamingResponse

from hymical_forms import export, models, storage
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
from hymical_forms.ingestion import ENDPOINT_ID_MAX_LENGTH
from hymical_forms.models import utcnow
from hymical_forms.webhooks import DeliveryState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["submission management"])

UNAUTHENTICATED = {
    "model": ErrorResponse,
    "description": "Missing or invalid management API key",
}

EndpointFilter = Annotated[
    str | None,
    Query(
        max_length=ENDPOINT_ID_MAX_LENGTH,
        description="Only submissions for this endpoint. Omit for every endpoint.",
    ),
]

ReceivedAfterFilter = Annotated[
    datetime | None,
    Query(
        description=(
            "Only submissions received strictly after this ISO 8601 instant. "
            "A submission received exactly on it is excluded."
        ),
    ),
]

ReceivedBeforeFilter = Annotated[
    datetime | None,
    Query(
        description=(
            "Only submissions received strictly before this ISO 8601 instant. "
            "A submission received exactly on it is excluded."
        ),
    ),
]

FormatQuery = Annotated[
    str | None,
    Query(
        description="The export format, `json` or `csv`. Defaults to `json`.",
    ),
]

JSON_FORMAT = "json"
CSV_FORMAT = "csv"
EXPORT_FORMATS = (JSON_FORMAT, CSV_FORMAT)


class SubmissionNotFound(ApiError):
    """
    raised when a management route addresses a submission that does not exist
    """

    status_code = HTTPStatus.NOT_FOUND
    code = "submission_not_found"

    def __init__(self, submission_id: str) -> None:
        """
        name the submission identifier that could not be resolved
        :param submission_id: the identifier taken from the request path
        """
        # A submission that retention has removed answers exactly the same way as
        # one that never existed, which is right: from outside, both mean this
        # service does not hold it.
        super().__init__(
            f"No submission with the ID {submission_id!r} exists.",
            details={"submission_id": submission_id},
        )


class InvalidTimeRange(ApiError):
    """
    raised when the two time bounds cannot both be satisfied
    """

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "invalid_time_range"

    def __init__(self) -> None:
        """
        state the relationship the two bounds have to be in
        """
        # Refused rather than answered with an empty page, because a range that
        # cannot match anything is a mistake in the request rather than a fact
        # about the data, and answering it with nothing hides the mistake.
        super().__init__(
            "received_after must be strictly earlier than received_before. Both bounds "
            "are exclusive, so a range where they are equal matches nothing.",
            details={"fields": ["received_after", "received_before"]},
        )


class UnsupportedExportFormat(ApiError):
    """
    raised when an export was asked for in a format this service does not write
    """

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "unsupported_export_format"

    def __init__(self, requested: str) -> None:
        """
        report the rejected format alongside the supported ones
        :param requested: the format the caller asked for
        """
        super().__init__(
            f"Exports are written as {' or '.join(EXPORT_FORMATS)}.",
            details={"field": "format", "supported": list(EXPORT_FORMATS)},
        )


class ExportTooLarge(ApiError):
    """
    raised when a filter matches more submissions than one export may return
    """

    # A 422 rather than a 413: the request is well formed and it is the filter,
    # which is part of the request, that has to change. Refused rather than
    # truncated, so an export is either everything that matched or an error, and
    # never a quietly incomplete file somebody archives.
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "export_too_large"

    def __init__(self, maximum: int) -> None:
        """
        report the ceiling the filter would have exceeded
        :param maximum: the most submissions one export may return
        """
        super().__init__(
            f"This filter matches more than {maximum} submissions, which is the most one "
            "export may return. Narrow it with received_after and received_before, or "
            "with endpoint_id, and export the range in parts.",
            details={"limit": maximum},
        )


class DeliverySummary(BaseModel):
    """
    the delivery a submission owes, as seen from the submission
    """

    # Enough to tell an operator whether this submission reached its destination
    # and where to look next, and no more. The signing secret is not here, and
    # neither is the attempt history: that is what the delivery routes are for.
    id: str = Field(description="Opaque identifier of the delivery carrying this submission.")
    state: DeliveryState = Field(description="Where that delivery has got to.")
    attempt_count: int = Field(
        description="How many requests have ever been made for that delivery."
    )


class SubmissionSummary(BaseModel):
    """
    one stored submission as a listing reports it
    """

    # There is no ``fields`` property on this model at all, rather than one that
    # is sometimes populated. A listing that cannot name the submitted values
    # cannot leak them, however the route above it changes.
    id: str = Field(description="Opaque identifier for this submission.")
    endpoint_id: str = Field(description="The endpoint the submission was addressed to.")
    received_at: datetime = Field(description="UTC timestamp of when the API accepted the body.")
    field_count: int = Field(
        description=(
            "Number of name/value pairs the submission carried. A field submitted three "
            "times counts three times, the same as it does on the ingestion response."
        )
    )
    idempotent: bool = Field(
        description=(
            "True when the submission was sent with an Idempotency-Key header. The key "
            "itself is never returned: anyone holding it can resolve it to this "
            "submission through the public ingestion route."
        )
    )
    delivery: DeliverySummary | None = Field(
        description=(
            "The webhook delivery this submission owes, or null when its endpoint has "
            "no webhook. A submission owes at most one delivery."
        )
    )


class SubmissionDetail(SubmissionSummary):
    """
    one stored submission together with the values it carried
    """

    fields: dict[str, list[str]] = Field(
        description=(
            "The submitted fields, in the order they arrived, with every value as a "
            "list. A field submitted once is a one-element list rather than a bare "
            "string, and a repeated field keeps its values in the order they were sent."
        )
    )


class SubmissionExport(BaseModel):
    """
    one submission as a JSON export writes it

    Declared for the OpenAPI document rather than used to build a response: the
    export is written incrementally by :mod:`hymical_forms.export` so that a
    large one does not have to exist in memory before it can be sent.
    """

    id: str = Field(description="Opaque identifier for this submission.")
    endpoint_id: str = Field(description="The endpoint the submission was addressed to.")
    received_at: datetime = Field(description="UTC timestamp of when the API accepted the body.")
    fields: dict[str, list[str]] = Field(
        description="The submitted fields, with every value as a list, exactly as stored."
    )


class SubmissionExportDocument(BaseModel):
    """
    the document a JSON export writes
    """

    submissions: list[SubmissionExport] = Field(
        description="Every matching submission, newest first."
    )


class SubmissionPage(BaseModel):
    """
    one page of stored submissions
    """

    items: list[SubmissionSummary] = Field(
        description="The submissions on this page, newest first."
    )
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` to read the next page, or null when this is certainly "
            "the last one. A full page always carries a cursor, so the final request "
            "of a walk returns an empty page."
        )
    )


@router.get(
    "/submissions",
    summary="List stored submissions",
    responses={
        401: UNAUTHENTICATED,
        422: {"model": ErrorResponse, "description": "Invalid filter, page size or cursor"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def list_submissions(
    session: SessionDep,
    principal: ManagementKeyDep,
    endpoint_id: EndpointFilter = None,
    received_after: ReceivedAfterFilter = None,
    received_before: ReceivedBeforeFilter = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    cursor: CursorQuery = None,
) -> SubmissionPage:
    """
    read a page of the submissions this service holds
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :param endpoint_id: only submissions for this endpoint, or None for every endpoint
    :param received_after: only submissions strictly newer than this instant
    :param received_before: only submissions strictly older than this instant
    :param limit: the most submissions to return
    :param cursor: the previous page's cursor, or None to read the first page
    :returns: one page of submission summaries, newest first
    :raises InvalidTimeRange: if the two time bounds cannot both be satisfied
    :raises InvalidCursor: if the cursor does not continue from a known submission
    """
    filters = _filters(endpoint_id, received_after, received_before)
    try:
        page = storage.list_submissions(session, filters=filters, limit=limit, after=cursor)
    except storage.UnknownCursor as exc:
        raise InvalidCursor() from exc

    return SubmissionPage(
        items=[_summary(record) for record in page],
        next_cursor=next_cursor([record.submission.id for record in page], limit=limit),
    )


# Declared before ``/submissions/{submission_id}``, because routes are matched in
# the order they are added and ``export`` would otherwise be read as an
# identifier and answered with a 404.
@router.get(
    "/submissions/export",
    summary="Export stored submissions",
    response_class=Response,
    responses={
        200: {
            "model": SubmissionExportDocument,
            "description": (
                "The matching submissions as a downloadable file, in the requested "
                "format. Sent with a Content-Disposition attachment filename."
            ),
            # The CSV form has no schema worth writing beyond its media type: its
            # columns depend on which field names the export turns out to hold.
            "content": {"text/csv": {"schema": {"type": "string"}}},
        },
        401: UNAUTHENTICATED,
        422: {
            "model": ErrorResponse,
            "description": "Invalid filter or format, or more matches than one export may return",
        },
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def export_submissions(
    request: Request,
    session: SessionDep,
    principal: ManagementKeyDep,
    endpoint_id: EndpointFilter = None,
    received_after: ReceivedAfterFilter = None,
    received_before: ReceivedBeforeFilter = None,
    format: FormatQuery = None,
) -> Response:
    """
    export the submissions a filter matches, as a downloadable file
    :param request: the incoming request, read for the configuration and session factory
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :param endpoint_id: only submissions for this endpoint, or None for every endpoint
    :param received_after: only submissions strictly newer than this instant
    :param received_before: only submissions strictly older than this instant
    :param format: the format to write, ``json`` or ``csv``, defaulting to ``json``
    :returns: the export, offered as an attachment
    :raises UnsupportedExportFormat: if the requested format is not one this service writes
    :raises InvalidTimeRange: if the two time bounds cannot both be satisfied
    :raises ExportTooLarge: if the filter matches more submissions than one export may return
    """
    chosen = (format or JSON_FORMAT).lower()
    if chosen not in EXPORT_FORMATS:
        raise UnsupportedExportFormat(chosen)

    settings: Settings = request.app.state.settings
    maximum = settings.export_max_submissions
    filters = _filters(endpoint_id, received_after, received_before)

    # Counted before anything is written, because once a body has started there
    # is no way back to an error response. The count is bounded by the maximum
    # itself, so an enormous filter costs a walk of that many index entries
    # rather than a walk of the table. Past the check it is an exact number, and
    # the log below is the one place it is worth having.
    matched = storage.count_submissions(session, filters=filters, ceiling=maximum + 1)
    if matched > maximum:
        raise ExportTooLarge(maximum)

    # Who asked, for what, and how much came out. Not one field name and not one
    # field value: an export is the one request whose whole purpose is to move
    # form content, and duplicating it into the log would undo that boundary.
    logger.info(
        "%d submission(s) exported as %s by management key %s (%s), endpoint %s",
        matched,
        chosen,
        principal.key_id,
        principal.name,
        endpoint_id or "any",
    )
    if chosen == CSV_FORMAT:
        return _csv_export(session, filters=filters, maximum=maximum)
    return _json_export(request, filters=filters, maximum=maximum)


@router.get(
    "/submissions/{submission_id}",
    summary="Inspect one stored submission",
    responses={
        401: UNAUTHENTICATED,
        404: {"model": ErrorResponse, "description": "No such submission"},
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def get_submission(
    submission_id: str,
    session: SessionDep,
    principal: ManagementKeyDep,
) -> SubmissionDetail:
    """
    read one submission and the values it carried
    :param submission_id: submission identifier taken from the request path
    :param session: the session this request does its database work through
    :param principal: the management key this request authenticated as
    :returns: the submission, its fields and the delivery it owes if it owes one
    :raises SubmissionNotFound: if no submission holds that identifier
    """
    record = storage.get_submission(session, submission_id)
    if record is None:
        raise SubmissionNotFound(submission_id)

    # Nothing about the fields is logged, here or anywhere below. They are
    # returned to the caller that authenticated and asked for them, and that is
    # the whole of where they go.
    return SubmissionDetail(
        **_summary(record).model_dump(),
        fields={name: list(values) for name, values in record.submission.fields.items()},
    )


def _json_export(
    request: Request, *, filters: storage.SubmissionFilter, maximum: int
) -> StreamingResponse:
    """
    build a streamed JSON export of the matching submissions
    :param request: the incoming request, read for the application's session factory
    :param filters: the endpoint and time bounds to export within
    :param maximum: the most submissions the export may return
    :returns: a streaming response carrying the export as an attachment
    """
    # The body is produced after this function returns, by which time the
    # session the request was served on may already have been closed. So the
    # stream opens one of its own and closes it when the generator finishes,
    # which is also what happens if the client disconnects part way through.
    factory: sessionmaker[Session] = request.app.state.session_factory

    def body() -> Iterator[str]:
        """
        write the document as the rows arrive
        :returns: an iterator over the pieces of the JSON document
        """
        with factory() as stream_session:
            rows = storage.stream_submissions(stream_session, filters=filters, limit=maximum)
            yield from export.json_document(_exportable(row) for row in rows)

    filename = export.export_filename(JSON_FORMAT, now=utcnow())
    return StreamingResponse(
        body(),
        media_type=export.JSON_MEDIA_TYPE,
        headers={"Content-Disposition": export.content_disposition(filename)},
    )


def _csv_export(session: Session, *, filters: storage.SubmissionFilter, maximum: int) -> Response:
    """
    build a CSV export of the matching submissions
    :param session: the session this request does its database work through
    :param filters: the endpoint and time bounds to export within
    :param maximum: the most submissions the export may return
    :returns: a response carrying the export as an attachment
    """
    # Not streamed, and the reason is in the format rather than in the plumbing:
    # a CSV header is the union of every field name in the export, which is only
    # known once the last row has been read. The set is already bounded by the
    # export maximum, which is what makes building it in one pass safe.
    rows = [
        _exportable(row)
        for row in storage.stream_submissions(session, filters=filters, limit=maximum)
    ]
    filename = export.export_filename(CSV_FORMAT, now=utcnow())
    return Response(
        content=export.csv_document(rows),
        media_type=export.CSV_MEDIA_TYPE,
        headers={"Content-Disposition": export.content_disposition(filename)},
    )


def _exportable(submission: models.Submission) -> export.ExportedSubmission:
    """
    narrow a stored submission to the parts an export writes
    :param submission: the persisted submission
    :returns: the submission's identity and content, with no internal columns
    """
    return export.ExportedSubmission(
        id=submission.id,
        endpoint_id=submission.endpoint_id,
        received_at=submission.received_at,
        fields=submission.fields,
    )


def _filters(
    endpoint_id: str | None,
    received_after: datetime | None,
    received_before: datetime | None,
) -> storage.SubmissionFilter:
    """
    gather and check the bounds a submission read asked for
    :param endpoint_id: only submissions for this endpoint, or None for every endpoint
    :param received_after: only submissions strictly newer than this instant
    :param received_before: only submissions strictly older than this instant
    :returns: the filter the storage layer applies
    :raises InvalidTimeRange: if the two bounds cannot both be satisfied
    """
    if (
        received_after is not None
        and received_before is not None
        and received_after >= received_before
    ):
        raise InvalidTimeRange()
    return storage.SubmissionFilter(
        endpoint_id=endpoint_id,
        received_after=received_after,
        received_before=received_before,
    )


def _summary(record: storage.SubmissionRecord) -> SubmissionSummary:
    """
    render a submission for a management listing
    :param record: the submission and the delivery it owes, if it owes one
    :returns: the submission's metadata, carrying none of its submitted values
    """
    # ``fields`` is read here only to be counted. The stored mapping came along
    # with the row because a page is one query, and this is the one place the
    # boundary between metadata and content is a decision in code rather than a
    # column that was never selected. The model above has nowhere to put them.
    submission = record.submission
    return SubmissionSummary(
        id=submission.id,
        endpoint_id=submission.endpoint_id,
        received_at=submission.received_at,
        field_count=sum(len(values) for values in submission.fields.values()),
        idempotent=submission.idempotency_key is not None,
        delivery=_delivery(record.delivery),
    )


def _delivery(delivery: models.WebhookDelivery | None) -> DeliverySummary | None:
    """
    render the delivery a submission owes, if it owes one
    :param delivery: the persisted delivery, or None when the endpoint has no webhook
    :returns: the delivery's state, or None when there is no delivery
    """
    if delivery is None:
        return None
    return DeliverySummary(
        id=delivery.id,
        state=DeliveryState(delivery.state),
        attempt_count=delivery.attempts,
    )
