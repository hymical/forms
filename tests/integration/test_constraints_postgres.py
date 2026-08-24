"""
the database invariants, enforced by PostgreSQL rather than by Python

The application leans on these: idempotency, one delivery per submission, and
the paired-column checks are all written as constraints on purpose, because a
concurrent request can slip past any check the application makes for itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models
from hymical_forms.webhooks import DeliveryState
from integration.support import seed_endpoint

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def a_submission(
    submission_id: str = "sub_one",
    *,
    idempotency_key: str | None = None,
    payload_fingerprint: str | None = None,
) -> models.Submission:
    """
    build a submission row for the default endpoint
    :param submission_id: the identifier to give it
    :param idempotency_key: the retry key, if any
    :param payload_fingerprint: the content digest, if any
    :returns: an unsaved submission row
    """
    return models.Submission(
        id=submission_id,
        endpoint_id="contact-form",
        received_at=NOW,
        fields={"email": ["dev@example.com"]},
        idempotency_key=idempotency_key,
        payload_fingerprint=payload_fingerprint,
    )


def a_delivery(delivery_id: str, submission_id: str) -> models.WebhookDelivery:
    """
    build a pending delivery row
    :param delivery_id: the identifier to give it
    :param submission_id: the submission it belongs to
    :returns: an unsaved delivery row
    """
    return models.WebhookDelivery(
        id=delivery_id,
        submission_id=submission_id,
        destination_url="https://example.invalid/hook",
        signing_secret="whsec_" + "a" * 64,
        state=DeliveryState.PENDING,
        attempts=0,
        next_attempt_at=NOW,
        created_at=NOW,
    )


def test_an_endpoint_id_is_unique(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(models.Endpoint(id="contact-form", name="Another", is_active=True))
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_idempotency_key_is_unique_per_endpoint(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_submission("sub_one", idempotency_key="k" * 16, payload_fingerprint="f" * 64))
        session.commit()

        session.add(a_submission("sub_two", idempotency_key="k" * 16, payload_fingerprint="f" * 64))
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_same_key_is_allowed_on_another_endpoint(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_endpoint(session)
        seed_endpoint(session, "waitlist")
        session.add(a_submission("sub_one", idempotency_key="k" * 16, payload_fingerprint="f" * 64))
        second = a_submission("sub_two", idempotency_key="k" * 16, payload_fingerprint="f" * 64)
        second.endpoint_id = "waitlist"
        session.add(second)

        session.commit()

        assert session.get(models.Submission, "sub_two") is not None


def test_submissions_without_a_key_are_unrestricted(sessions: sessionmaker[Session]) -> None:
    """
    PostgreSQL treats NULLs in a unique constraint as distinct, which is relied on
    :param sessions: factory handing out independent connections
    """
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_submission("sub_one"))
        session.add(a_submission("sub_two"))
        session.add(a_submission("sub_three"))

        session.commit()

        assert session.get(models.Submission, "sub_three") is not None


def test_a_submission_owes_at_most_one_delivery(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_submission("sub_one"))
        session.add(a_delivery("whd_one", "sub_one"))
        session.commit()

        session.add(a_delivery("whd_two", "sub_one"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_webhook_url_cannot_exist_without_its_secret(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        session.add(
            models.Endpoint(
                id="contact-form",
                name="Contact form",
                is_active=True,
                webhook_url="https://example.invalid/hook",
                webhook_secret=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_half_an_idempotency_identity_is_refused(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_submission("sub_one", idempotency_key="k" * 16))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_terminal_delivery_must_have_a_completion_time(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_submission("sub_one"))
        delivery = a_delivery("whd_one", "sub_one")
        delivery.state = DeliveryState.DELIVERED
        delivery.completed_at = None
        session.add(delivery)
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_pending_delivery_must_not_have_a_completion_time(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_submission("sub_one"))
        delivery = a_delivery("whd_one", "sub_one")
        delivery.completed_at = NOW
        session.add(delivery)
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_submission_cannot_reference_a_missing_endpoint(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        session.add(a_submission("sub_one"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_delivery_cannot_reference_a_missing_submission(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed_endpoint(session)
        session.add(a_delivery("whd_one", "sub_missing"))
        with pytest.raises(IntegrityError):
            session.commit()
