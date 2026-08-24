"""
a real local HTTP server that records the webhook deliveries it receives

Tests point the service at this rather than at a mocked transport, so the bytes
asserted on are the bytes that actually crossed a socket, and so timeouts and
refused connections are the real thing rather than a simulated exception.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType


@dataclass
class ReceivedWebhook:
    """
    one request the recorder was sent
    """

    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class WebhookRecorder:
    """
    a local server that records deliveries and answers however a test wants
    """

    status: int = 200
    delay_seconds: float = 0.0
    received: list[ReceivedWebhook] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """
        the destination a webhook should be configured with
        :returns: the absolute URL this recorder answers on
        """
        assert self._server is not None, "the recorder is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/hook"

    def start(self) -> WebhookRecorder:
        """
        bind to a free port on the loopback interface and begin serving
        :returns: this recorder, now running
        """
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # The capitalised name is the one BaseHTTPRequestHandler dispatches to.
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                recorder.received.append(
                    ReceivedWebhook(
                        path=self.path,
                        headers={key.lower(): value for key, value in self.headers.items()},
                        body=self.rfile.read(length),
                    )
                )
                # Sleeping after reading the body means the client has finished
                # sending and is waiting on a response, which is what a read
                # timeout is meant to catch.
                if recorder.delay_seconds:
                    time.sleep(recorder.delay_seconds)
                self.send_response(recorder.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                """
                silence the per-request logging that would otherwise flood the test output
                :param format: the message template, ignored
                :param args: the message arguments, ignored
                """

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        # The default poll interval makes every shutdown wait half a second,
        # which across a test module is most of the runtime.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """
        stop serving and release the port
        """
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> WebhookRecorder:
        """
        start the recorder for the duration of a with block
        :returns: this recorder, now running
        """
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """
        stop the recorder when the with block ends
        :param exc_type: type of any exception being propagated
        :param exc: any exception being propagated
        :param traceback: traceback of any exception being propagated
        """
        self.stop()


def unused_local_url() -> str:
    """
    build a loopback URL with nothing listening on it
    :returns: a URL whose connection will be refused
    """
    # Binding and closing hands back a port the OS considers free, so connecting
    # to it is refused immediately rather than hanging.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/hook"
