"""
webhook rules: destination validation, signing secrets, payload and signature

Nothing in this module performs I/O. Building and signing a payload is kept
apart from sending it so that the bytes which get signed are provably the bytes
that go on the wire, and so both can be tested without a network.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from hymical_forms.ingestion import Submission

SIGNATURE_HEADER = "Hymical-Signature"
SIGNATURE_VERSION = "v1"

SUBMISSION_RECEIVED_EVENT = "submission.received"

WEBHOOK_SECRET_PREFIX = "whsec_"
WEBHOOK_SECRET_MAX_LENGTH = len(WEBHOOK_SECRET_PREFIX) + 64

WEBHOOK_URL_MAX_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")

DELIVERY_ATTEMPT_ID_PREFIX = "att_"
DELIVERY_ATTEMPT_ID_MAX_LENGTH = len(DELIVERY_ATTEMPT_ID_PREFIX) + 32

# Failure text is written by whatever the destination did, so it is attacker
# influenced and has to be bounded before it reaches a column.
DELIVERY_ERROR_MAX_LENGTH = 500


class DeliveryOutcome(StrEnum):
    """
    the coarse result of one webhook delivery attempt
    """

    # These strings are a public contract: they are stored, and reported back on
    # the submission response. Exception class names deliberately never appear.
    SUCCEEDED = "succeeded"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """
    what one delivery attempt produced
    """

    outcome: DeliveryOutcome
    response_status: int | None = None
    error: str | None = None


class WebhookUrlRejected(Exception):
    """
    raised when a webhook destination is not one this service will send to
    """

    def __init__(self, reason: str) -> None:
        """
        record why the destination was refused
        :param reason: short phrase completing "the webhook URL ..."
        """
        super().__init__(reason)
        self.reason = reason


def validate_webhook_url(url: str, *, allow_private_targets: bool = False) -> None:
    """
    check that a destination is one this service is willing to send to
    :param url: the destination the caller wants submissions delivered to
    :param allow_private_targets: whether to permit loopback and private addresses
    :raises WebhookUrlRejected: if the destination is malformed or not permitted
    """
    if len(url) > WEBHOOK_URL_MAX_LENGTH:
        raise WebhookUrlRejected(f"must be at most {WEBHOOK_URL_MAX_LENGTH} characters")

    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        # urlsplit rejects malformed IPv6 literals and out-of-range ports here.
        raise WebhookUrlRejected("must be a well-formed URL") from exc

    if parts.scheme not in ALLOWED_SCHEMES:
        raise WebhookUrlRejected("must use the http or https scheme")
    if not host:
        raise WebhookUrlRejected("must include a host")

    if not allow_private_targets and _is_internal_host(host):
        raise WebhookUrlRejected(
            "must not address a loopback, private, link-local or otherwise internal host"
        )


def _is_internal_host(host: str) -> bool:
    """
    report whether a host literal obviously names the server's own network
    :param host: the host taken from the destination URL
    :returns: True if the host is one submissions must not be delivered to
    """
    # Only literals are judged. A name is not resolved here, so a hostname that
    # resolves to a private address still passes; see the SSRF note in the README.
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True

    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False

    # ``::ffff:127.0.0.1`` is a loopback address wearing an IPv6 costume, and the
    # IPv6 flags do not see through it.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def new_signing_secret() -> str:
    """
    generate a signing secret for a webhook destination
    :returns: a prefixed, cryptographically random secret
    """
    return f"{WEBHOOK_SECRET_PREFIX}{secrets.token_hex(32)}"


def new_delivery_attempt_id() -> str:
    """
    generate an opaque identifier for a delivery attempt
    :returns: a fresh attempt id such as ``att_1f0c9a...``
    """
    return f"{DELIVERY_ATTEMPT_ID_PREFIX}{uuid.uuid4().hex}"


def build_payload(submission: Submission) -> dict[str, Any]:
    """
    build the event body describing a stored submission
    :param submission: the submission that was accepted
    :returns: the payload to serialize and send
    """
    # Repeated values stay lists, exactly as they are stored, so a receiver never
    # has to guess whether a field is single or multi valued.
    return {
        "type": SUBMISSION_RECEIVED_EVENT,
        "submission": {
            "id": submission.id,
            "endpoint_id": submission.endpoint_id,
            "received_at": _rfc3339(submission.received_at),
            "fields": {name: list(values) for name, values in submission.fields.items()},
        },
    }


def serialize_payload(payload: dict[str, Any]) -> bytes:
    """
    render a payload to the exact bytes that will be signed and sent
    :param payload: the event body to serialize
    :returns: the UTF-8 encoded JSON body
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    """
    compute the signature header value for an outbound body
    :param body: the exact bytes that will be transmitted
    :param secret: the destination's signing secret
    :returns: the header value, such as ``v1=<hex digest>``
    """
    # Versioned from the start, so a future scheme can add another element
    # without breaking receivers that only understand v1.
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def _rfc3339(moment: datetime) -> str:
    """
    render a timestamp the way the rest of the API renders timestamps
    :param moment: the instant to render
    :returns: an RFC 3339 timestamp in UTC, ending in Z
    """
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
