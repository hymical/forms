"""
retention against a real PostgreSQL database

SQLite enforces the foreign keys this suite runs on, but only because the test
harness turns enforcement on, and it has no opinion about what a real batch of
deletes costs. The point of running retention here is that the ``ON DELETE SET
NULL`` behaviour, the batching and the export queries meet the database this
service is actually deployed against.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models, storage
from hymical_forms.webhooks import DeliveryState
from integration.support import seed_endpoint

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)

ENDPOINT_ID = "contact-form"


def seed_submission(
    session: Session,
    *,
    received_at: datetime,
    fields: dict[str, list[str]] | None = None,
    delivery_state: DeliveryState | None = None,
    attempts: int = 0,
) -> str:
    """
    insert one submission, optionally owing a delivery with a history
    :param session: the session to insert through
    :param received_at: the instant the submission claims it was accepted
    :param fields: the submitted values, defaulting to one email field
    :param delivery_state: the state of the delivery it owes, or None to owe none
    :param attempts: how many requests have been made for that delivery
    :returns: the submission identifier
    """
    submission_id = f"sub_{uuid.uuid4().hex}"
    delivery_id = f"whd_{uuid.uuid4().hex}"
    terminal = delivery_state in (DeliveryState.DELIVERED, DeliveryState.FAILED)
    session.add(
        models.Submission(
            id=submission_id,
            endpoint_id=ENDPOINT_ID,
            received_at=received_at,
            fields=fields if fields is not None else {"email": ["dev@example.com"]},
        )
    )
    if delivery_state is not None:
        session.add(
            models.WebhookDelivery(
                id=delivery_id,
                submission_id=submission_id,
                endpoint_id=ENDPOINT_ID,
                destination_url="https://example.invalid/hook",
                signing_secret="whsec_" + "a" * 64,
                state=delivery_state,
                attempts=attempts,
                cycle_attempts=attempts,
                next_attempt_at=received_at,
                created_at=received_at,
                completed_at=received_at if terminal else None,
            )
        )
        session.flush()
        for number in range(1, attempts + 1):
            session.add(
                models.DeliveryAttempt(
                    id=f"att_{uuid.uuid4().hex}",
                    delivery_id=delivery_id,
                    submission_id=submission_id,
                    attempt_number=number,
                    destination_url="https://example.invalid/hook",
                    attempted_at=received_at,
                    outcome="http_error",
                    response_status=503,
                )
            )
    session.commit()
    return submission_id


# --- foreign keys ------------------------------------------------------------


def test_deleting_a_submission_unlinks_its_delivery(sessions: sessionmaker[Session]) -> None:
    """
    the database, not the sweep, is what keeps the history when the content goes
    :param sessions: factory handing out independent connections
    """
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        seed_submission(
            session,
            received_at=CUTOFF - timedelta(days=1),
            delivery_state=DeliveryState.DELIVERED,
            attempts=2,
        )

    with sessions() as session:
        removed = storage.delete_expired_submissions(
            session, before=CUTOFF, batch_size=100, max_batches=10
        )

    assert removed == 1
    with sessions() as session:
        delivery = session.scalars(select(models.WebhookDelivery)).one()
        attempts = list(session.scalars(select(models.DeliveryAttempt)))
    assert delivery.submission_id is None
    assert delivery.state == DeliveryState.DELIVERED
    assert delivery.endpoint_id == ENDPOINT_ID
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert all(attempt.submission_id is None for attempt in attempts)


def test_a_delivery_still_cannot_name_a_submission_that_never_existed(
    sessions: sessionmaker[Session],
) -> None:
    """
    loosening the column to nullable must not have loosened the reference itself
    :param sessions: factory handing out independent connections
    """
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        session.add(
            models.WebhookDelivery(
                id=f"whd_{uuid.uuid4().hex}",
                submission_id="sub_does_not_exist",
                endpoint_id=ENDPOINT_ID,
                destination_url="https://example.invalid/hook",
                signing_secret="whsec_" + "a" * 64,
                state=DeliveryState.PENDING,
                attempts=0,
                cycle_attempts=0,
                next_attempt_at=NOW,
                created_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_protected_submission_survives_a_sweep(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        pending = seed_submission(
            session,
            received_at=CUTOFF - timedelta(days=365),
            delivery_state=DeliveryState.PENDING,
        )
        failed = seed_submission(
            session,
            received_at=CUTOFF - timedelta(days=365),
            delivery_state=DeliveryState.FAILED,
            attempts=5,
        )

    with sessions() as session:
        removed = storage.delete_expired_submissions(
            session, before=CUTOFF, batch_size=100, max_batches=10
        )

    assert removed == 0
    with sessions() as session:
        assert set(session.scalars(select(models.Submission.id))) == {pending, failed}


# --- batching ----------------------------------------------------------------


def test_a_sweep_clears_a_backlog_in_batches(sessions: sessionmaker[Session]) -> None:
    """
    many small committed transactions rather than one that holds locks over everything
    :param sessions: factory handing out independent connections
    """
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        for index in range(25):
            seed_submission(session, received_at=CUTOFF - timedelta(days=1 + index))

    with sessions() as session:
        removed = storage.delete_expired_submissions(
            session, before=CUTOFF, batch_size=4, max_batches=100
        )

    assert removed == 25
    with sessions() as session:
        assert session.scalars(select(models.Submission.id)).all() == []


def test_a_sweep_that_is_cut_short_has_still_committed_what_it_removed(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        for index in range(10):
            seed_submission(session, received_at=CUTOFF - timedelta(days=1 + index))

    with sessions() as session:
        removed = storage.delete_expired_submissions(
            session, before=CUTOFF, batch_size=3, max_batches=2
        )

    assert removed == 6
    # Read on a different connection, so what is asserted is what was committed
    # rather than what one session happens to be holding.
    with sessions() as session:
        assert len(list(session.scalars(select(models.Submission.id)))) == 4


# --- the management surface over real postgres -------------------------------


def test_submissions_are_listed_over_http(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        older = seed_submission(session, received_at=NOW - timedelta(hours=2))
        newer = seed_submission(session, received_at=NOW)

    body = pg_client.get("/submissions").json()

    assert [item["id"] for item in body["items"]] == [newer, older]


def test_a_filtered_page_narrows_over_http(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        seed_submission(session, received_at=NOW - timedelta(days=2))
        wanted = seed_submission(session, received_at=NOW)

    body = pg_client.get(
        "/submissions",
        params={
            "endpoint_id": ENDPOINT_ID,
            "received_after": (NOW - timedelta(days=1)).isoformat(),
        },
    ).json()

    assert [item["id"] for item in body["items"]] == [wanted]


def test_an_export_streams_the_filtered_set_over_http(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    """
    the export opens a session of its own, which only a real backend proves works
    :param pg_client: an API client on the migrated PostgreSQL database
    :param sessions: factory handing out independent connections
    """
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        for index in range(30):
            seed_submission(
                session,
                received_at=NOW - timedelta(minutes=index),
                fields={"email": [f"dev{index}@example.com"], "topics": ["api", "billing"]},
            )

    response = pg_client.get("/submissions/export")

    assert response.status_code == 200
    exported = response.json()["submissions"]
    assert len(exported) == 30
    assert exported[0]["fields"]["topics"] == ["api", "billing"]


def test_a_csv_export_works_over_http(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        seed_submission(session, received_at=NOW, fields={"naam": ["Zoë"], "topics": ["api"]})

    response = pg_client.get("/submissions/export", params={"format": "csv"})

    assert response.status_code == 200
    header, row = list(csv.reader(io.StringIO(response.text)))
    assert header == ["submission_id", "endpoint_id", "received_at", "naam", "topics"]
    assert row[3] == '["Zoë"]'
    assert row[4] == '["api"]'


def test_a_delivery_whose_submission_was_swept_is_still_listed(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    """
    operational history has to stay readable after the form content has gone
    :param pg_client: an API client on the migrated PostgreSQL database
    :param sessions: factory handing out independent connections
    """
    with sessions() as session:
        seed_endpoint(session, ENDPOINT_ID)
        seed_submission(
            session,
            received_at=CUTOFF - timedelta(days=1),
            delivery_state=DeliveryState.DELIVERED,
            attempts=1,
        )
    with sessions() as session:
        storage.delete_expired_submissions(session, before=CUTOFF, batch_size=100, max_batches=10)

    listed = pg_client.get("/deliveries", params={"endpoint_id": ENDPOINT_ID}).json()["items"]

    assert len(listed) == 1
    assert listed[0]["submission_id"] is None
    assert listed[0]["endpoint_id"] == ENDPOINT_ID
    assert listed[0]["state"] == DeliveryState.DELIVERED

    detail = pg_client.get(f"/deliveries/{listed[0]['id']}").json()
    assert len(detail["attempts"]) == 1
