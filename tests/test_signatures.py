"""
webhook signatures, checked the way the documentation tells a receiver to check them

The service never verifies a signature itself: it signs, and the receiver
verifies. So the useful assertions are about what a receiver following the
documented recipe will accept and refuse, and about which repeated requests a
receiver will see as the same event and therefore has to deduplicate.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from hymical_forms.ingestion import Submission
from hymical_forms.webhooks import (
    SIGNATURE_VERSION,
    build_payload,
    new_signing_secret,
    serialize_payload,
    sign,
)

SECRET = "whsec_" + "ab" * 32


def documented_verify(raw_body: bytes, header: str, secret: str) -> bool:
    """
    the receiver recipe from docs/guides/webhooks.md, verbatim in behaviour
    :param raw_body: the exact bytes the receiver was sent
    :param header: the Hymical-Signature header value
    :param secret: the endpoint's signing secret
    :returns: True if the receiver should accept the request
    """
    version, _, digest = header.partition("=")
    if version != "v1":
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def submission(submission_id: str = "sub_" + "1" * 32) -> Submission:
    """
    build a fixed submission, so its serialized bytes are fully determined
    :param submission_id: the identifier to give it
    :returns: the submission
    """
    return Submission(
        id=submission_id,
        endpoint_id="contact-form",
        received_at=datetime(2026, 8, 24, 14, 34, 27, 651841, tzinfo=UTC),
        fields={"email": ("dev@example.com",), "note": ("héllo",)},
    )


def signed(sub: Submission, secret: str = SECRET) -> tuple[bytes, str]:
    """
    produce the body and header the worker would send for a submission
    :param sub: the submission being delivered
    :param secret: the signing secret
    :returns: the body and its signature header value
    """
    body = serialize_payload(build_payload(sub))
    return body, sign(body, secret)


def test_the_signature_is_hmac_sha256_over_the_exact_body() -> None:
    body, header = signed(submission())

    expected = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert header == f"{SIGNATURE_VERSION}={expected}"
    assert documented_verify(body, header, SECRET)


def test_a_body_altered_by_one_byte_is_refused() -> None:
    body, header = signed(submission())
    tampered = body.replace(b"dev@example.com", b"dev@example.con")

    assert tampered != body
    assert not documented_verify(tampered, header, SECRET)


def test_a_signature_from_another_secret_is_refused() -> None:
    body, header = signed(submission(), secret=new_signing_secret())

    assert not documented_verify(body, header, SECRET)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda header: "",
        lambda header: header.split("=", 1)[1],
        lambda header: "v2=" + header.split("=", 1)[1],
        lambda header: header[:-1],
        lambda header: header + "0",
        lambda header: header.upper(),
        lambda header: "v1=",
    ],
)
def test_a_malformed_signature_header_is_refused(mangle: Callable[[str], str]) -> None:
    body, header = signed(submission())

    assert not documented_verify(body, mangle(header), SECRET)


def test_a_redelivery_is_byte_for_byte_the_same_event() -> None:
    # Delivery is at-least-once, so a receiver will see some events twice. A
    # retry has to be recognisably the same event, signature included, or the
    # receiver could not deduplicate it on the submission id it carries.
    first = signed(submission())
    again = signed(submission())

    assert first == again


def test_a_signature_cannot_be_moved_onto_another_event() -> None:
    # The submission id is inside the signed bytes, so a captured request cannot
    # be replayed under a fresh id to slip past a receiver deduplicating on it.
    body, header = signed(submission("sub_" + "1" * 32))
    other, _ = signed(submission("sub_" + "2" * 32))

    assert not documented_verify(other, header, SECRET)
    assert documented_verify(body, header, SECRET)
