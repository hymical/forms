"""
the outbound half of webhook delivery: one HTTP attempt, no retries

This module is the only place the service makes an outbound request. It is kept
separate from :mod:`hymical_forms.webhooks` so that the rules and the payload can
be tested without a network, and separate from the database layer so that no
transaction is ever held open across a call to somebody else's server.
"""

from __future__ import annotations

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


async def deliver(
    client: httpx2.AsyncClient, *, url: str, secret: str, body: bytes
) -> DeliveryResult:
    """
    make one attempt to deliver a signed payload
    :param client: the shared outbound client
    :param url: the destination to post to
    :param secret: the destination's signing secret
    :param body: the exact bytes to sign and transmit
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

    try:
        response = await client.post(url, content=body, headers=headers)
    except httpx2.TimeoutException as exc:
        return DeliveryResult(DeliveryOutcome.TIMEOUT, error=_describe("timed out", exc))
    except httpx2.RequestError as exc:
        return DeliveryResult(
            DeliveryOutcome.NETWORK_ERROR, error=_describe("could not connect", exc)
        )

    if 200 <= response.status_code < 300:
        return DeliveryResult(DeliveryOutcome.SUCCEEDED, response_status=response.status_code)

    return DeliveryResult(
        DeliveryOutcome.HTTP_ERROR,
        response_status=response.status_code,
        error=f"destination responded with HTTP {response.status_code}",
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
