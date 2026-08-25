"""
form ingestion endpoint: ``POST /f/{endpoint_id}``
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta
from http import HTTPStatus

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from python_multipart.exceptions import ParseError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

from hymical_forms import storage
from hymical_forms.config import Settings
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError, ErrorResponse, error_response
from hymical_forms.ingestion import (
    ENDPOINT_ID_RULE,
    IDEMPOTENCY_KEY_RULE,
    build_submission,
    is_valid_endpoint_id,
    is_valid_idempotency_key,
    payload_fingerprint,
)
from hymical_forms.models import utcnow
from hymical_forms.ratelimit import (
    FORWARDED_FOR_HEADER,
    Limiter,
    RateLimit,
    RateLimitDecision,
    client_address,
    ip_subject,
    seconds_until_window_ends,
    window_start,
)
from hymical_forms.webhooks import WebhookTarget

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

URLENCODED = "application/x-www-form-urlencoded"
MULTIPART = "multipart/form-data"
SUPPORTED_MEDIA_TYPES = (URLENCODED, MULTIPART)

# Content-Type values are echoed back to help developers debug their form tags,
# but only ever a bounded prefix of what the client sent.
_MEDIA_TYPE_ECHO_LIMIT = 128

# Fixed windows leave rows behind, and pretending that is harmless would be
# untrue: one row per source address per window is unbounded in exactly the
# traffic this feature exists to survive. A background process for one DELETE
# would be a whole thing to deploy, so a small fraction of submission attempts
# pay for it instead. At one in a hundred, a service quiet enough to accumulate
# nothing sweeps rarely and a service busy enough to accumulate a lot sweeps
# often, which is the right shape without a schedule to tune.
_SWEEP_PROBABILITY = 0.01

# How many of the longest configured window to keep before sweeping. More than
# one, so a sweep can never take a window that is still being counted in, and
# small, because these rows answer nothing once their window has ended.
_SWEEP_RETAINED_WINDOWS = 2

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


class RateLimitExceeded(ApiError):
    """
    raised when a public submission attempt exhausted one of the traffic limits
    """

    status_code = HTTPStatus.TOO_MANY_REQUESTS
    code = "rate_limit_exceeded"

    def __init__(self, decision: RateLimitDecision) -> None:
        """
        report which budget ran out and how long it takes to refill
        :param decision: the exhausted budget's decision for this attempt
        """
        # Which limiter tripped is included on purpose. An integrator whose form
        # is being flooded from many addresses and one whose own client is
        # looping need to do completely different things about it, and the answer
        # is not something the response could keep hidden anyway: anyone can tell
        # the two apart by trying the same endpoint from a second address. What is
        # not included is the subject, the counter, or anything naming a column.
        super().__init__(
            f"Too many submission attempts. Try again in {decision.retry_after_seconds} seconds.",
            details={
                "scope": str(decision.limiter),
                "limit": decision.limit.requests,
                "window_seconds": decision.limit.window_seconds,
                "retry_after_seconds": decision.retry_after_seconds,
            },
        )
        self.retry_after_seconds = decision.retry_after_seconds

    def as_response(self) -> JSONResponse:
        """
        render this error with the wait a 429 is not a complete answer without
        :returns: a JSONResponse carrying the envelope and a Retry-After header
        """
        # Built per instance rather than through ``ApiError.headers``, which is a
        # ClassVar for statuses whose header never varies. This one carries a
        # number worked out for the request being refused.
        return error_response(
            status_code=self.status_code,
            code=self.code,
            message=self.message,
            details=self.details,
            headers={"Retry-After": str(self.retry_after_seconds)},
        )


class DeliveryStatus(BaseModel):
    """
    whether this submission owes a webhook delivery
    """

    queued: bool = Field(
        description=(
            "True when a durable webhook delivery exists for this submission. False "
            "when the endpoint has no webhook. A delivery is queued once and is not "
            "queued again by an idempotent replay, so a replay of a webhook-enabled "
            "submission still reports true."
        )
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
        description="Whether a webhook delivery is owed for this submission."
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
        429: {
            "model": ErrorResponse,
            "description": "Rate limited by source address or by endpoint",
        },
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
    # The order of this handler is the abuse-protection design, so it is worth
    # stating plainly. The body-size cap has already run, in middleware, before
    # this function exists: an oversized body is refused without being read and
    # without touching the database at all. Then the source address spends a unit
    # of its budget, before anything is looked up. Then the endpoint is resolved,
    # and if it exists it spends a unit of its own budget. Only after both
    # limiters have allowed the attempt is the body parsed and stored.
    #
    # This handler must stay ``async`` to stream the body, so each blocking
    # database call is handed to a worker thread instead of stalling the loop.
    settings: Settings = request.app.state.settings
    now = utcnow()

    # Charged before the identifier is even checked for syntax, because an
    # attempt costs this service something whether or not it turns out to be
    # well formed, and because the cheapest place to refuse a flood is the
    # earliest one. Every attempt that reaches this handler is charged, including
    # ones that go on to be refused as malformed, unsupported or unacceptable.
    if settings.rate_limit_enabled:
        await _consume(
            session,
            limiter=Limiter.IP,
            subject=_address_subject(request, settings),
            limit=settings.ip_rate_limit(),
            now=now,
        )

    if not is_valid_endpoint_id(endpoint_id):
        raise InvalidEndpointId()

    # The endpoint is resolved before the body is parsed, so an unknown endpoint
    # costs one indexed lookup rather than a full parse of a body we would throw
    # away.
    endpoint = await run_in_threadpool(storage.get_endpoint, session, endpoint_id)
    if endpoint is None:
        # A guessed identifier spends the guesser's own budget and nothing else.
        # Charging a per-endpoint counter here would mean inventing a row for
        # every string an attacker tries, which hands them control of how much
        # this table grows.
        raise EndpointNotFound(endpoint_id)

    # Read the endpoint's configuration off the row now, while the session is
    # known to be clean. The limiter below commits, and storing the submission
    # can roll back to settle an idempotency race; a rollback expires loaded
    # objects, so touching the endpoint later would silently issue a refresh
    # query from this async handler.
    is_active = endpoint.is_active
    webhook_url = endpoint.webhook_url
    webhook_secret = endpoint.webhook_secret

    # Charged for a resolved endpoint whether or not the attempt is going to be
    # accepted, so that an endpoint somebody has disabled cannot be used as a
    # free target either. Its budget is shared by every source, which is what
    # makes it the limit that answers an attack spread across many addresses.
    if settings.rate_limit_enabled:
        await _consume(
            session,
            limiter=Limiter.ENDPOINT,
            subject=endpoint_id,
            limit=settings.endpoint_rate_limit(),
            now=now,
        )
        await _sweep_old_windows(session, settings, now=now)

    if not is_active:
        raise EndpointInactive(endpoint_id)

    media_type = _media_type(request.headers.get("content-type"))
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise UnsupportedMediaType(media_type)

    idempotency_key = _idempotency_key(request)

    submission = build_submission(
        endpoint_id,
        await _parse_form(request, media_type, settings),
        max_fields=settings.max_fields,
        max_field_name_length=settings.max_field_name_length,
        max_field_value_length=settings.max_field_value_length,
    )

    # The submission and, if the endpoint has a webhook, the durable obligation to
    # deliver it are committed together. Nothing outbound happens here: once this
    # returns, a worker owns the delivery, and a crash in this process can no
    # longer lose a delivery that was implicitly promised by a 202.
    #
    # The commit happens inside the handler, not in the session dependency's
    # teardown, so that a failure still becomes an error response. Teardown runs
    # after the response has been sent, where raising could no longer change it.
    webhook = (
        WebhookTarget(url=webhook_url, secret=webhook_secret)
        if webhook_url is not None and webhook_secret is not None
        else None
    )
    try:
        stored = await run_in_threadpool(
            storage.store_submission,
            session,
            submission,
            now=submission.received_at,
            idempotency_key=idempotency_key,
            payload_fingerprint=(
                payload_fingerprint(submission.fields) if idempotency_key else None
            ),
            webhook=webhook,
        )
    except storage.IdempotencyKeyReused as exc:
        raise IdempotencyConflict(exc.endpoint_id, exc.idempotency_key) from exc

    # A replay answers with the original submission's identity and timestamp, so
    # a client that retried after a lost response ends up describing one event.
    # It reports the same queued state as the original, because the delivery that
    # request created is still the one that is owed.
    return SubmissionAccepted(
        submission_id=stored.submission.id,
        endpoint_id=stored.submission.endpoint_id,
        received_at=stored.submission.received_at,
        field_count=stored.submission.field_count,
        idempotent_replay=stored.replayed,
        delivery=DeliveryStatus(queued=webhook is not None),
    )


async def _consume(
    session: Session,
    *,
    limiter: Limiter,
    subject: str,
    limit: RateLimit,
    now: datetime,
) -> None:
    """
    spend one unit of a budget and refuse the attempt if it was already spent
    :param session: the session this request does its database work through
    :param limiter: which budget is being drawn from
    :param subject: the value that budget is keyed by
    :param limit: how many attempts the window allows and how long it lasts
    :param now: the instant this attempt arrived
    :raises RateLimitExceeded: if the subject has already spent this window's budget
    """
    # The unit is spent first and judged afterwards, which is what makes a
    # refused attempt still count against the sender. Deciding first and then
    # charging only the attempts that passed would let a saturated subject keep
    # sending for free, and free is the one thing abuse traffic must not be.
    start = window_start(now, limit.window_seconds)
    used = await run_in_threadpool(
        storage.consume_rate_limit,
        session,
        limiter=limiter,
        subject=subject,
        window_start=start,
    )
    decision = RateLimitDecision(
        limiter=limiter,
        limit=limit,
        used=used,
        retry_after_seconds=seconds_until_window_ends(now, start, limit.window_seconds),
    )
    if not decision.allowed:
        raise RateLimitExceeded(decision)


async def _sweep_old_windows(session: Session, settings: Settings, *, now: datetime) -> None:
    """
    occasionally remove counters whose window ended long ago
    :param session: the session this request does its database work through
    :param settings: active configuration, read for the window lengths
    :param now: the instant this attempt arrived
    """
    if random.random() >= _SWEEP_PROBABILITY:
        return

    oldest = max(
        settings.rate_limit_ip_window_seconds,
        settings.rate_limit_endpoint_window_seconds,
    )
    before = now - timedelta(seconds=_SWEEP_RETAINED_WINDOWS * oldest)
    try:
        await run_in_threadpool(storage.delete_expired_rate_limit_counters, session, before=before)
    except SQLAlchemyError:
        # Housekeeping, and it runs after the decision this request needed has
        # already been made and committed. A database that cannot tidy up must
        # not be able to turn an otherwise fine submission into a 503, and the
        # rollback is what hands the rest of the handler a usable session.
        session.rollback()
        logger.warning("could not sweep expired rate limit counters")


def _address_subject(request: Request, settings: Settings) -> str:
    """
    work out the value this request's source address is counted under
    :param request: the incoming request
    :param settings: active configuration, read for the proxy and privacy settings
    :returns: a hex digest of the resolved client address
    """
    address = client_address(
        peer=request.client.host if request.client is not None else None,
        forwarded_for=request.headers.get(FORWARDED_FOR_HEADER),
        trusted_proxy_hops=settings.trusted_proxy_hops,
    )
    return ip_subject(address, settings.rate_limit_ip_secret)


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
