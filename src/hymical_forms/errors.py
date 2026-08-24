"""The single JSON error envelope used by every non-2xx response.

Every error the API can produce — raised by our own code, by FastAPI's request
validation, or by Starlette's routing — is rendered as::

    {"error": {"code": "...", "message": "...", "details": {...}}}

``code`` is a stable, machine-readable string; ``message`` is a human-readable
sentence; ``details`` is present only when there is something concrete to add
(the limit that was exceeded, the field at fault). Nothing in the envelope
exposes internal types, stack frames, or file paths.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, ClassVar

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hymical_forms.ingestion import SubmissionRejected


class ErrorDetail(BaseModel):
    """The body of an error response."""

    code: str = Field(description="Stable, machine-readable error identifier.")
    message: str = Field(description="Human-readable explanation of the failure.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context, such as the limit that was exceeded.",
    )


class ErrorResponse(BaseModel):
    """The envelope returned for every error."""

    error: ErrorDetail


class ApiError(Exception):
    """An error that maps directly onto the public error envelope.

    Subclasses fix ``status_code`` and ``code``; instances supply the message and
    any structured details.
    """

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def as_response(self) -> JSONResponse:
        return error_response(
            status_code=self.status_code,
            code=self.code,
            message=self.message,
            details=self.details,
        )


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a JSON response in the standard error envelope."""
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=payload.model_dump(exclude_none=True))


def register_exception_handlers(app: FastAPI) -> None:
    """Route every error class the app can raise through the shared envelope."""
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(SubmissionRejected, _handle_submission_rejected)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


# Starlette types every handler as ``(Request, Exception) -> Response``, so each
# handler re-narrows the exception it was registered for.


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return exc.as_response()


async def _handle_submission_rejected(request: Request, exc: Exception) -> Response:
    """Render a domain rejection.

    Every ingestion rule failure is a well-formed request carrying an
    unacceptable submission, which is exactly what 422 describes.
    """
    assert isinstance(exc, SubmissionRejected)
    return error_response(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    """Render routing-level errors (unknown paths, wrong methods) in the envelope."""
    assert isinstance(exc, StarletteHTTPException)
    return error_response(
        status_code=exc.status_code,
        code=_code_for_status(exc.status_code),
        message=str(exc.detail),
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="invalid_request",
        message="The request could not be validated.",
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """Return an opaque 500 rather than letting an internal error reach the client."""
    return error_response(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="The request could not be processed.",
    )


def _code_for_status(status_code: int) -> str:
    """Derive an error code from a status code, e.g. 405 -> ``method_not_allowed``."""
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        return "http_error"
    return phrase.lower().replace("-", " ").replace(" ", "_")
