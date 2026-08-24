"""
the single JSON error envelope used by every non-2xx response

Every error the API can produce, whether raised by our own code, by FastAPI's
request validation, or by Starlette's routing, is rendered as::

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
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hymical_forms.ingestion import SubmissionRejected


class ErrorDetail(BaseModel):
    """
    the body of an error response
    """

    code: str = Field(description="Stable, machine-readable error identifier.")
    message: str = Field(description="Human-readable explanation of the failure.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context, such as the limit that was exceeded.",
    )


class ErrorResponse(BaseModel):
    """
    the envelope returned for every error
    """

    error: ErrorDetail


class ApiError(Exception):
    """
    an error that maps directly onto the public error envelope
    """

    # Subclasses fix ``status_code`` and ``code``; instances supply the message
    # and any structured details.
    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        """
        record the message and context for an error response
        :param message: human-readable explanation of the failure
        :param details: optional structured context, such as the limit that was exceeded
        """
        super().__init__(message)
        self.message = message
        self.details = details

    def as_response(self) -> JSONResponse:
        """
        render this error in the shared envelope
        :returns: a JSONResponse carrying the envelope and this error's status code
        """
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
    """
    build a JSON response in the standard error envelope
    :param status_code: HTTP status code to return
    :param code: stable, machine-readable error identifier
    :param message: human-readable explanation of the failure
    :param details: optional structured context, omitted from the body when absent
    :returns: a JSONResponse carrying the envelope
    """
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=payload.model_dump(exclude_none=True))


def register_exception_handlers(app: FastAPI) -> None:
    """
    route every error class the app can raise through the shared envelope
    :param app: the application to register the handlers on
    """
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(SubmissionRejected, _handle_submission_rejected)
    app.add_exception_handler(SQLAlchemyError, _handle_storage_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


# Starlette types every handler as ``(Request, Exception) -> Response``, so each
# handler re-narrows the exception it was registered for.


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    """
    render an error raised by our own HTTP layer
    :param request: the request being handled
    :param exc: the raised exception, always an ApiError
    :returns: the envelope response
    """
    assert isinstance(exc, ApiError)
    return exc.as_response()


async def _handle_submission_rejected(request: Request, exc: Exception) -> Response:
    """
    render a domain rejection
    :param request: the request being handled
    :param exc: the raised exception, always a SubmissionRejected
    :returns: the envelope response, with a 422 status
    """
    # Every ingestion rule failure is a well-formed request carrying an
    # unacceptable submission, which is exactly what 422 describes.
    assert isinstance(exc, SubmissionRejected)
    return error_response(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def _handle_storage_error(request: Request, exc: Exception) -> Response:
    """
    render a database failure without describing it
    :param request: the request being handled
    :param exc: the raised exception, always a SQLAlchemyError
    :returns: the envelope response, with a 503 status
    """
    # Driver messages carry table names, SQL text and sometimes connection
    # details, so none of the exception reaches the client. 503 rather than 500
    # because the request itself was fine and retrying it may well succeed.
    return error_response(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        code="storage_unavailable",
        message="The submission could not be stored. Try again shortly.",
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    """
    render routing-level errors such as unknown paths and wrong methods
    :param request: the request being handled
    :param exc: the raised exception, always a Starlette HTTPException
    :returns: the envelope response
    """
    assert isinstance(exc, StarletteHTTPException)
    return error_response(
        status_code=exc.status_code,
        code=_code_for_status(exc.status_code),
        message=str(exc.detail),
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> Response:
    """
    render a request that FastAPI could not validate
    :param request: the request being handled
    :param exc: the raised exception, always a RequestValidationError
    :returns: the envelope response, with a 422 status
    """
    assert isinstance(exc, RequestValidationError)
    # Only the location and pydantic's short explanation are relayed. The raw
    # error carries the offending input, which may be user data we should not
    # reflect back, and internal type names that mean nothing to a caller.
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]) or None,
            "issue": error["msg"],
        }
        for error in exc.errors()
    ]
    return error_response(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="invalid_request",
        message="The request body could not be validated.",
        details={"fields": fields} if fields else None,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """
    return an opaque 500 rather than letting an internal error reach the client
    :param request: the request being handled
    :param exc: the unhandled exception, deliberately not described to the client
    :returns: the envelope response, with a 500 status
    """
    return error_response(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="The request could not be processed.",
    )


def _code_for_status(status_code: int) -> str:
    """
    derive an error code from a status code, so that 405 gives ``method_not_allowed``
    :param status_code: HTTP status code to name
    :returns: the status phrase in snake case, or ``http_error`` if unrecognised
    """
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        return "http_error"
    return phrase.lower().replace("-", " ").replace(" ", "_")
