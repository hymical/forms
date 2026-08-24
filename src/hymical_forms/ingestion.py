"""Ingestion domain rules: endpoint identifiers and submission normalization.

This module is deliberately free of HTTP concepts. It answers two questions —
"is this a well-formed endpoint identifier?" and "is this set of name/value pairs
an acceptable submission?" — and leaves status codes and wire formats to the API
layer.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
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


def is_valid_endpoint_id(value: str) -> bool:
    """Report whether ``value`` is a syntactically valid endpoint identifier.

    Interval 1 has no endpoint registry, so any syntactically valid identifier is
    treated as addressable.
    """
    return (
        ENDPOINT_ID_MIN_LENGTH <= len(value) <= ENDPOINT_ID_MAX_LENGTH
        and _ENDPOINT_ID_PATTERN.fullmatch(value) is not None
    )


class SubmissionRejected(Exception):
    """A submission violated an ingestion rule and must not be accepted."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class Submission:
    """A validated form submission in its internal representation.

    Repeated field names are preserved as ordered tuples because HTML forms use
    them for checkbox groups and multi-selects; collapsing them would silently
    discard user input.
    """

    id: str
    endpoint_id: str
    received_at: datetime
    fields: dict[str, tuple[str, ...]]

    @property
    def field_count(self) -> int:
        """The number of name/value pairs the submission carries."""
        return sum(len(values) for values in self.fields.values())


def new_submission_id() -> str:
    """Generate an opaque, prefixed submission identifier."""
    return f"{SUBMISSION_ID_PREFIX}{uuid.uuid4().hex}"


def build_submission(
    endpoint_id: str,
    items: Sequence[tuple[str, str]],
    *,
    max_fields: int,
    max_field_name_length: int,
    max_field_value_length: int,
) -> Submission:
    """Validate parsed form pairs and normalize them into a :class:`Submission`.

    ``items`` is the ordered sequence of name/value pairs exactly as parsed from
    the request body, including repeats.

    Raises:
        SubmissionRejected: if the submission is empty or breaches a limit.
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
