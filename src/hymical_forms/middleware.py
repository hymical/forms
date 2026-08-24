"""ASGI middleware protecting the ingestion boundary."""

from __future__ import annotations

from http import HTTPStatus

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hymical_forms.errors import ApiError


class RequestBodyTooLarge(ApiError):
    """The request body exceeded the configured maximum."""

    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    code = "request_body_too_large"

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Request body exceeds the limit of {limit} bytes.",
            details={"limit_bytes": limit},
        )


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds ``max_bytes``.

    Starlette buffers request bodies without an upper bound, so the cap has to sit
    in front of the form parsers rather than inside a route handler. Requests that
    declare an oversized ``Content-Length`` are refused before a single body byte
    is read; the rest are cut off as soon as the running total crosses the limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await RequestBodyTooLarge(self.max_bytes).as_response()(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside the application, so the registered ApiError
                    # handler renders it in the standard envelope.
                    raise RequestBodyTooLarge(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)


def _declared_content_length(scope: Scope) -> int | None:
    """Read ``Content-Length`` from the raw ASGI scope, ignoring unparseable values."""
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
