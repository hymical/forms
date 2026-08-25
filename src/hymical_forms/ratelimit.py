"""
traffic rate limiting rules for public form ingestion

Nothing in this module performs I/O or knows about HTTP or the database. It
answers which fixed window an instant falls in, how long that window has left,
which address a request should be counted against, and what value each limiter
is keyed by. :mod:`hymical_forms.storage` owns the atomic counter, and the
ingestion route owns the order the two limiters run in.

The algorithm is a fixed window on purpose. It is one row and one statement per
decision, every process computes the same boundary from the same clock, and what
it does is explainable in a sentence. Its known weakness is the boundary: a
client that spends a whole window just before it ends and a whole window just
after can make twice the configured requests across those two windows. A sliding
window or a token bucket would smooth that out, at the cost of either keeping a
log of request instants or a second column that has to be refilled from a
timestamp, and neither is worth it for a first layer of abuse protection whose
job is to stop unbounded traffic rather than to shape well-behaved traffic.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# One column holds both kinds of subject. An endpoint identifier is at most 64
# characters and a hashed address is a 64-character hex digest, so they fit the
# same width without either being padded or truncated.
SUBJECT_MAX_LENGTH = 64
LIMITER_MAX_LENGTH = 16

FORWARDED_FOR_HEADER = "X-Forwarded-For"

# What a request is counted under when the ASGI server reported no peer address.
# Such requests share one bucket rather than escaping the limiter: a request
# nothing can attribute is exactly what abuse looks like, so the safe reading is
# the strict one.
UNKNOWN_CLIENT = "unknown"


class Limiter(StrEnum):
    """
    which budget a rate limit decision is drawn from
    """

    # These strings are stored and are reported back in the body of a 429, so
    # they are a public contract rather than an implementation detail.
    IP = "ip"
    ENDPOINT = "endpoint"


@dataclass(frozen=True, slots=True)
class RateLimit:
    """
    how many attempts one subject may make within one fixed window
    """

    requests: int
    window_seconds: int


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """
    the outcome of spending one unit of one subject's budget
    """

    limiter: Limiter
    limit: RateLimit
    used: int
    retry_after_seconds: int

    @property
    def allowed(self) -> bool:
        """
        report whether the attempt that spent this unit may proceed
        :returns: True if the budget was not already exhausted
        """
        # The unit is spent either way, so the request that takes a subject
        # exactly to its limit is the last one allowed through.
        return self.used <= self.limit.requests


def window_start(now: datetime, window_seconds: int) -> datetime:
    """
    find the start of the fixed window an instant falls in
    :param now: the instant to place
    :param window_seconds: how long each window lasts
    :returns: the window's start, as a timezone-aware UTC timestamp
    """
    # Floored against the Unix epoch rather than against anything process-local,
    # so every API process derives the same boundary from the same clock and the
    # counter they share is the counter they both meant to write.
    elapsed = int(now.timestamp())
    return datetime.fromtimestamp(elapsed - elapsed % window_seconds, UTC)


def seconds_until_window_ends(now: datetime, start: datetime, window_seconds: int) -> int:
    """
    work out how long a refused client has to wait for a fresh window
    :param now: the instant the decision was made
    :param start: the start of the window the decision was made in
    :param window_seconds: how long each window lasts
    :returns: whole seconds remaining, never fewer than one
    """
    # Rounded up and floored at one, because RFC 9110 wants whole seconds and
    # because answering with a truncated value would invite a retry the same
    # window is still going to refuse.
    remaining = (start + timedelta(seconds=window_seconds) - now).total_seconds()
    return max(1, math.ceil(remaining))


def client_address(*, peer: str | None, forwarded_for: str | None, trusted_proxy_hops: int) -> str:
    """
    decide which address a request should be rate limited by
    :param peer: socket peer address the ASGI server reported, or None if it reported none
    :param forwarded_for: raw ``X-Forwarded-For`` header value, or None when absent
    :param trusted_proxy_hops: how many proxies of your own stand in front of this process
    :returns: the address the per-IP limiter counts against
    """
    # The socket peer is the default because it is the one address in a request
    # that the client did not write. ``X-Forwarded-For`` is attacker-controlled
    # text until a proxy you run appends to it, so treating it as authoritative
    # by default would hand every client its own private rate limit for free.
    #
    # It is read only when an operator has said how many hops of their own to
    # skip, and it is counted from the right: each proxy in the chain appends the
    # address it saw, so with one trusted proxy the last entry is what that proxy
    # observed, with two it is the second from last, and everything to the left of
    # that was written by somebody who is not yours to trust.
    if trusted_proxy_hops > 0 and forwarded_for:
        hops = [entry.strip() for entry in forwarded_for.split(",") if entry.strip()]
        if len(hops) >= trusted_proxy_hops:
            return hops[-trusted_proxy_hops]
        # Fewer entries than configured means the chain is not what the operator
        # described, so the header is discarded rather than half believed.
    return peer or UNKNOWN_CLIENT


def ip_subject(address: str, secret: str | None) -> str:
    """
    reduce a client address to the value a counter may be keyed by
    :param address: the client address resolved for this request
    :param secret: server-side secret to key the digest with, or None for a plain digest
    :returns: a hex SHA-256 digest of the address
    """
    # The raw address is never stored, never logged and never returned. What is
    # stored is a fixed-width digest of it, which is enough for the only thing the
    # limiter needs: telling one source from another within a window.
    #
    # Without a secret this is obfuscation and is documented as exactly that. The
    # IPv4 space is small enough to enumerate, so anybody holding the table can
    # recover the addresses in it; the digest only keeps them out of a casual dump
    # and out of anything that reads the column by eye.
    #
    # With a secret it is a genuine one-way mapping, and the usual objection to
    # introducing a second secret does not apply here. These counters live for one
    # window, so changing or losing the secret costs at most one window of
    # accounting rather than invalidating anything durable. That is the whole
    # operational problem it solves, and it is why the secret is optional rather
    # than required.
    if secret is None:
        return hashlib.sha256(address.encode("utf-8")).hexdigest()
    return hmac.new(secret.encode("utf-8"), address.encode("utf-8"), hashlib.sha256).hexdigest()
