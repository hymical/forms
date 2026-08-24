"""The internal submission representation.

These tests pin the shape the rest of the system will eventually persist and
deliver, which the HTTP acknowledgement only summarises.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hymical_forms.ingestion import SubmissionRejected, build_submission

LIMITS = {
    "max_fields": 100,
    "max_field_name_length": 128,
    "max_field_value_length": 16384,
}


def test_normalizes_pairs_into_a_field_mapping() -> None:
    submission = build_submission(
        "contact-form",
        [("email", "dev@example.com"), ("message", "hello")],
        **LIMITS,
    )

    assert submission.endpoint_id == "contact-form"
    assert submission.fields == {"email": ("dev@example.com",), "message": ("hello",)}
    assert submission.field_count == 2


def test_preserves_repeated_names_in_submission_order() -> None:
    submission = build_submission(
        "contact-form",
        [("topic", "billing"), ("email", "dev@example.com"), ("topic", "api")],
        **LIMITS,
    )

    assert submission.fields == {
        "topic": ("billing", "api"),
        "email": ("dev@example.com",),
    }
    assert submission.field_count == 3


def test_stamps_each_submission_with_an_id_and_utc_timestamp() -> None:
    before = datetime.now(UTC)
    submission = build_submission("contact-form", [("email", "a@b.co")], **LIMITS)
    after = datetime.now(UTC)

    assert submission.id.startswith("sub_")
    assert submission.received_at.tzinfo is not None
    assert before <= submission.received_at <= after


def test_rejects_a_submission_with_no_fields() -> None:
    with pytest.raises(SubmissionRejected) as raised:
        build_submission("contact-form", [], **LIMITS)

    assert raised.value.code == "empty_submission"
