"""
middleware protecting the ingestion boundary at the ASGI layer
"""

from __future__ import annotations

from http import HTTPStatus

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hymical_forms.errors import ApiError


class RequestBodyTooLarge(ApiError):
    """
    raised when a request body exceeds the configured maximum
    """

    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    code = "request_body_too_large"

    def __init__(self, limit: int) -> None:
        """
        record the limit the body overran
        :param limit: largest request body accepted, in bytes
        """
        super().__init__(
            f"Request body exceeds the limit of {limit} bytes.",
            details={"limit_bytes": limit},
        )


class BodySizeLimitMiddleware:
    """
    reject requests whose body exceeds a configured size
    """

    # Starlette buffers request bodies without an upper bound, so the cap has to
    # sit in front of the form parsers rather than inside a route handler.

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        """
        wrap an ASGI application with a request body size cap
        :param app: the ASGI application to wrap
        :param max_bytes: largest request body accepted, in bytes
        """
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        pass the request through, refusing any body over the limit
        :param scope: ASGI connection scope
        :param receive: ASGI callable yielding request messages
        :param send: ASGI callable accepting response messages
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # A request that declares an oversized Content-Length is refused before a
        # single body byte is read.
        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await RequestBodyTooLarge(self.max_bytes).as_response()(scope, receive, send)
            return

        received = 0
        overflowed = False
        replaced = False

        async def limited_receive() -> Message:
            """
            read the next request message, cutting off an oversized body
            :returns: the next ASGI message
            :raises RequestBodyTooLarge: once the running body total crosses the limit
            """
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    overflowed = True
                    # Raised inside the application, so the registered ApiError
                    # handler renders it in the standard envelope.
                    raise RequestBodyTooLarge(self.max_bytes)
            return message

        async def guarded_send(message: Message) -> None:
            """
            make sure a body cut off for its size is answered as one
            :param message: the next ASGI response message the application sent
            """
            # FastAPI reads JSON bodies itself and converts any exception raised
            # while reading into its own 400, so on a JSON route the error raised
            # above never reaches the handler. Once the cap has been crossed, any
            # other answer is replaced with the 413 the cap is documented to give.
            nonlocal replaced
            if replaced:
                return
            if (
                overflowed
                and message["type"] == "http.response.start"
                and message["status"] != HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            ):
                replaced = True
                await RequestBodyTooLarge(self.max_bytes).as_response()(scope, receive, send)
                return
            await send(message)

        await self.app(scope, limited_receive, guarded_send)


def _declared_content_length(scope: Scope) -> int | None:
    """
    read the Content-Length header from the raw ASGI scope
    :param scope: ASGI connection scope
    :returns: the declared body length, or None when absent or unparseable
    """
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
