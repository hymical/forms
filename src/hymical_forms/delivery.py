"""
the outbound half of webhook delivery: one HTTP attempt, no retries

This module is the only place the service makes an outbound request. It is kept
separate from :mod:`hymical_forms.webhooks` so that the rules and the payload can
be tested without a network, and separate from the database layer so that no
transaction is ever held open across a call to somebody else's server.
"""

from __future__ import annotations

import asyncio

import httpx2

from hymical_forms import __version__
from hymical_forms.config import Settings
from hymical_forms.webhooks import (
    DELIVERY_ERROR_MAX_LENGTH,
    SIGNATURE_HEADER,
    DeliveryOutcome,
    DeliveryResult,
    sign,
)

USER_AGENT = f"Hymical-Forms/{__version__}"


def create_webhook_client(settings: Settings) -> httpx2.AsyncClient:
    """
    build the client every outbound webhook is sent through
    :param settings: active configuration, read for its timeouts
    :returns: a client with explicit timeouts, no redirects and no retries
    """
    # One client for the process, so connections are reused rather than
    # renegotiated per submission.
    #
    # Redirects are not followed. Beyond being surprising for a webhook, following
    # them would let a destination bounce the request to an address that the URL
    # validation refused, which is the usual way SSRF protection gets walked
    # around. A 3xx is therefore reported as an unsuccessful HTTP status.
    #
    # Transport retries are pinned to zero. This interval promises exactly one
    # attempt per submission, and a client that quietly retried would break that
    # promise for any receiver that is not idempotent.
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(
            connect=settings.webhook_connect_timeout_seconds,
            read=settings.webhook_read_timeout_seconds,
            write=settings.webhook_read_timeout_seconds,
            pool=settings.webhook_connect_timeout_seconds,
        ),
        follow_redirects=False,
        transport=httpx2.AsyncHTTPTransport(retries=0),
    )


def attempt_deadline_seconds(settings: Settings) -> float:
    """
    work out the longest one delivery attempt is allowed to take, start to finish
    :param settings: active configuration, read for its timeouts
    :returns: the overall deadline for one attempt, in seconds
    """
    # The client's timeouts each bound a single wait: one connect, one read of
    # whatever bytes arrive next. None of them bounds the attempt, so a
    # destination that answers one byte at a time could hold a request open for
    # as long as it liked, past any lease. Connecting plus one full read timeout
    # is the most an attempt is worth waiting for, and it is the figure the
    # operations guide tells operators to size the lease against.
    return settings.webhook_connect_timeout_seconds + settings.webhook_read_timeout_seconds


async def deliver(
    client: httpx2.AsyncClient,
    *,
    url: str,
    secret: str,
    body: bytes,
    deadline_seconds: float,
) -> DeliveryResult:
    """
    make one attempt to deliver a signed payload
    :param client: the shared outbound client
    :param url: the destination to post to
    :param secret: the destination's signing secret
    :param body: the exact bytes to sign and transmit
    :param deadline_seconds: the longest the whole attempt may take, see attempt_deadline_seconds
    :returns: the outcome, never raising for a destination that misbehaves
    """
    # ``content=body`` transmits these bytes verbatim. Passing the payload object
    # and letting the client serialize it would sign one encoding and send
    # another, and the receiver's signature check would fail for reasons nobody
    # could see.
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(body, secret),
        "User-Agent": USER_AGENT,
    }

    # The response is streamed and its body never read. The status is the whole
    # answer, and reading a body nobody looks at would let the destination decide
    # how much memory an attempt takes and how long it lasts.
    try:
        async with (
            asyncio.timeout(deadline_seconds),
            client.stream("POST", url, content=body, headers=headers) as response,
        ):
            status = response.status_code
    except httpx2.TimeoutException as exc:
        return DeliveryResult(DeliveryOutcome.TIMEOUT, error=_describe("timed out", exc))
    except httpx2.RequestError as exc:
        return DeliveryResult(
            DeliveryOutcome.NETWORK_ERROR, error=_describe("could not connect", exc)
        )
    except TimeoutError:
        return DeliveryResult(
            DeliveryOutcome.TIMEOUT,
            error=f"timed out: no response within the {deadline_seconds:g}s attempt deadline",
        )

    if 200 <= status < 300:
        return DeliveryResult(DeliveryOutcome.SUCCEEDED, response_status=status)

    return DeliveryResult(
        DeliveryOutcome.HTTP_ERROR,
        response_status=status,
        error=f"destination responded with HTTP {status}",
    )


def _describe(summary: str, exc: Exception) -> str:
    """
    build bounded failure text for a delivery that never reached a response
    :param summary: our own words for what went wrong
    :param exc: the transport error raised while trying
    :returns: a short message safe to store
    """
    # The exception's own text is useful to whoever debugs this later, but it is
    # shaped by the destination, so it is truncated before it reaches a column.
    detail = str(exc).strip()
    message = f"{summary}: {detail}" if detail else summary
    return message[:DELIVERY_ERROR_MAX_LENGTH]
