"""
ingestion domain rules: endpoint identifiers and submission normalization

This module is deliberately free of HTTP concepts. It answers two questions,
"is this a well-formed endpoint identifier?" and "is this set of name/value
pairs an acceptable submission?", and leaves status codes and wire formats to
the API layer.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

ENDPOINT_ID_MIN_LENGTH = 3
ENDPOINT_ID_MAX_LENGTH = 64

# Lowercase only, so that an endpoint ID has exactly one spelling. Hyphen and
# underscore are allowed inside, but an ID may not start or end with them.
_ENDPOINT_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?")

# C0/C1 controls and DEL. HTML permits almost anything else in a field name
# (``user[email]``, ``entry.42``, non-ASCII labels), so nothing else is rejected.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

SUBMISSION_ID_PREFIX = "sub_"

# ``sub_`` followed by a uuid4 in hex.
SUBMISSION_ID_MAX_LENGTH = len(SUBMISSION_ID_PREFIX) + 32

# Stated once so that every error mentioning the rule words it identically.
ENDPOINT_ID_RULE = (
    f"Endpoint IDs are {ENDPOINT_ID_MIN_LENGTH}-{ENDPOINT_ID_MAX_LENGTH} characters using "
    "lowercase letters, digits, '-' and '_', and must start and end with a letter or digit."
)

# An idempotency key is scoped to one endpoint, and this API is unauthenticated,
# so every client of an endpoint draws from the same key space. A short or
# predictable key would therefore collide with a stranger's submission, which is
# why the floor is high enough to force a random token rather than a counter.
IDEMPOTENCY_KEY_MIN_LENGTH = 16
IDEMPOTENCY_KEY_MAX_LENGTH = 255

# Printable ASCII with no spaces: covers UUIDs, hex, base64 and base64url, and
# keeps unbounded or unprintable header content out of the database.
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[!-~]+")

IDEMPOTENCY_KEY_RULE = (
    f"Idempotency keys are {IDEMPOTENCY_KEY_MIN_LENGTH}-{IDEMPOTENCY_KEY_MAX_LENGTH} printable "
    "ASCII characters with no spaces. Use a random value such as a UUID."
)

# A SHA-256 digest rendered as hex.
PAYLOAD_FINGERPRINT_LENGTH = 64


def is_valid_idempotency_key(value: str) -> bool:
    """
    report whether a header value is a usable idempotency key
    :param value: the raw ``Idempotency-Key`` header value
    :returns: True if the key is well formed
    """
    return (
        IDEMPOTENCY_KEY_MIN_LENGTH <= len(value) <= IDEMPOTENCY_KEY_MAX_LENGTH
        and _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is not None
    )


def payload_fingerprint(fields: Mapping[str, tuple[str, ...]]) -> str:
    """
    digest the submitted content so a retry can be recognised as the same request
    :param fields: the normalized fields of a submission
    :returns: a hex SHA-256 digest of the field content
    """
    # Only the fields are hashed. The generated submission ID and the received
    # timestamp differ on every attempt, so taking the mapping rather than the
    # whole submission makes their exclusion structural instead of a promise.
    #
    # The canonical form is a JSON array of ``[name, [values]]`` pairs, so it is
    # sensitive to field order and to repeated values, both of which this service
    # already treats as meaningful. JSON also makes the framing unambiguous:
    # concatenating names and values would let two different submissions produce
    # identical bytes. Nothing here depends on Python's randomized hashing, so
    # the digest is stable across processes and restarts.
    canonical = json.dumps(
        [[name, list(values)] for name, values in fields.items()],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_valid_endpoint_id(value: str) -> bool:
    """
    report whether a path segment is a syntactically valid endpoint identifier
    :param value: candidate identifier taken from the request path
    :returns: True if the identifier is well formed
    """
    # Interval 1 has no endpoint registry, so any syntactically valid identifier
    # is treated as addressable.
    return (
        ENDPOINT_ID_MIN_LENGTH <= len(value) <= ENDPOINT_ID_MAX_LENGTH
        and _ENDPOINT_ID_PATTERN.fullmatch(value) is not None
    )


class SubmissionRejected(Exception):
    """
    raised when a submission violates an ingestion rule and must not be accepted
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        """
        record why a submission was refused
        :param code: stable, machine-readable identifier for the broken rule
        :param message: human-readable explanation of the failure
        :param details: optional structured context, such as the limit that was exceeded
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class Submission:
    """
    a validated form submission in its internal representation
    """

    # Repeated field names are preserved as ordered tuples because HTML forms use
    # them for checkbox groups and multi-selects; collapsing them would silently
    # discard user input.
    id: str
    endpoint_id: str
    received_at: datetime
    fields: dict[str, tuple[str, ...]]

    @property
    def field_count(self) -> int:
        """
        count the name/value pairs the submission carries
        :returns: the total number of submitted values across all field names
        """
        return sum(len(values) for values in self.fields.values())


def new_submission_id() -> str:
    """
    generate an opaque, prefixed submission identifier
    :returns: a fresh submission id such as ``sub_1f0c9a...``
    """
    return f"{SUBMISSION_ID_PREFIX}{uuid.uuid4().hex}"


def build_submission(
    endpoint_id: str,
    items: Sequence[tuple[str, str]],
    *,
    max_fields: int,
    max_field_name_length: int,
    max_field_value_length: int,
) -> Submission:
    """
    validate parsed form pairs and normalize them into a submission
    :param endpoint_id: the endpoint the submission was addressed to
    :param items: ordered name/value pairs as parsed from the request body, repeats included
    :param max_fields: largest number of name/value pairs accepted
    :param max_field_name_length: largest field name accepted, in characters
    :param max_field_value_length: largest field value accepted, in characters
    :returns: the normalized submission
    :raises SubmissionRejected: if the submission is empty or breaches a limit
    """
    if len(items) > max_fields:
        raise SubmissionRejected(
            "too_many_fields",
            f"Submission carries {len(items)} fields, which exceeds the limit of {max_fields}.",
            {"limit": max_fields, "received": len(items)},
        )

    fields: dict[str, tuple[str, ...]] = {}
    for name, value in items:
        _validate_field_name(name, max_field_name_length)
        _validate_field_value(name, value, max_field_value_length)
        fields[name] = (*fields.get(name, ()), value)

    if not fields:
        raise SubmissionRejected(
            "empty_submission",
            "Submission contains no fields.",
        )

    return Submission(
        id=new_submission_id(),
        endpoint_id=endpoint_id,
        received_at=datetime.now(UTC),
        fields=fields,
    )


def _validate_field_name(name: str, max_length: int) -> None:
    """
    check a submitted field name against the name rules
    :param name: field name as submitted
    :param max_length: largest field name accepted, in characters
    :raises SubmissionRejected: if the name is empty, too long, or holds control characters
    """
    if not name:
        raise SubmissionRejected(
            "invalid_field_name",
            "Submission contains a field with an empty name.",
        )
    if len(name) > max_length:
        raise SubmissionRejected(
            "field_name_too_long",
            f"A field name exceeds the limit of {max_length} characters.",
            {"limit": max_length, "received": len(name)},
        )
    if _CONTROL_CHARS.search(name):
        raise SubmissionRejected(
            "invalid_field_name",
            "Submission contains a field name with control characters.",
        )


def _validate_field_value(name: str, value: str, max_length: int) -> None:
    """
    check a submitted field value against the value rules
    :param name: field name the value belongs to, used only in the error message
    :param value: field value as submitted
    :param max_length: largest field value accepted, in characters
    :raises SubmissionRejected: if the value is too long or holds a null byte
    """
    if len(value) > max_length:
        raise SubmissionRejected(
            "field_value_too_long",
            f"The value of field {name!r} exceeds the limit of {max_length} characters.",
            {"field": name, "limit": max_length, "received": len(value)},
        )
    if "\x00" in value:
        raise SubmissionRejected(
            "invalid_field_value",
            f"The value of field {name!r} contains a null byte.",
            {"field": name},
        )
