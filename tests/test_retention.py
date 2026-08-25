"""
the retention rule, and the operator command that applies it

The rule is the interesting part. A submission is only deleted once nothing will
read its content again, and what still needs it is decided by the state of the
delivery it owes rather than by its age alone: a queued webhook payload is built
from the submission at the moment it is sent, so anything still deliverable keeps
its submission however old that submission is.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from conftest import IsolatedSettings, build_settings
from hymical_forms import cli, models, storage
from hymical_forms.db import create_engine_from_url, create_session_factory
from hymical_forms.retention import RetentionDisabled, RetentionPolicy
from hymical_forms.schema import create_all
from hymical_forms.webhooks import DeliveryState

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)

ENDPOINT_ID = "contact-form"


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """
    provide a session factory on a migrated SQLite file the CLI will also find
    :param tmp_path: pytest fixture giving this test a directory of its own
    :param monkeypatch: pytest fixture used to point the CLI at that database
    :returns: an iterator yielding the session factory
    """
    # A file rather than an in-memory database, because the CLI opens its own
    # connection to whatever FORMS_DATABASE_URL names, exactly as it would in
    # real life.
    url = f"sqlite:///{tmp_path / 'forms.db'}"
    engine = create_engine_from_url(url)
    create_all(engine)

    monkeypatch.setenv("FORMS_DATABASE_URL", url)
    monkeypatch.setattr(cli, "Settings", IsolatedSettings)

    with create_session_factory(engine)() as session:
        session.add(models.Endpoint(id=ENDPOINT_ID, name="Contact form", created_at=NOW))
        session.commit()

    yield create_session_factory(engine)
    engine.dispose()


def seed(
    sessions: sessionmaker[Session],
    *,
    received_at: datetime,
    delivery_state: DeliveryState | None = None,
    attempts: int = 0,
) -> str:
    """
    insert one submission, optionally owing a delivery in a chosen state
    :param sessions: the session factory to insert through
    :param received_at: the instant the submission claims it was accepted
    :param delivery_state: the state of the delivery it owes, or None to owe none
    :param attempts: how many requests have been made for that delivery
    :returns: the submission identifier
    """
    submission_id = f"sub_{uuid.uuid4().hex}"
    delivery_id = f"whd_{uuid.uuid4().hex}"
    terminal = delivery_state in (DeliveryState.DELIVERED, DeliveryState.FAILED)
    with sessions() as session:
        session.add(
            models.Submission(
                id=submission_id,
                endpoint_id=ENDPOINT_ID,
                received_at=received_at,
                fields={"email": ["dev@example.com"]},
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


def remaining(sessions: sessionmaker[Session]) -> set[str]:
    """
    read which submissions are still stored
    :param sessions: the session factory to query through
    :returns: the identifiers still present
    """
    with sessions() as session:
        return set(session.scalars(select(models.Submission.id)))


def sweep(sessions: sessionmaker[Session], *, batch_size: int = 500) -> int:
    """
    run a retention sweep against the fixed cutoff
    :param sessions: the session factory to write through
    :param batch_size: how many submissions to delete per transaction
    :returns: how many submissions were deleted
    """
    with sessions() as session:
        return storage.delete_expired_submissions(
            session, before=CUTOFF, batch_size=batch_size, max_batches=100
        )


# --- the policy --------------------------------------------------------------


def test_an_unset_retention_keeps_everything_indefinitely() -> None:
    assert build_settings().retention_policy().enabled is False


def test_a_disabled_policy_has_no_cutoff_to_offer() -> None:
    with pytest.raises(RetentionDisabled):
        RetentionPolicy(days=0).cutoff(NOW)


def test_a_cutoff_is_the_configured_age_before_now() -> None:
    assert RetentionPolicy(days=30).cutoff(NOW) == NOW - timedelta(days=30)


def test_a_configured_retention_reaches_the_policy() -> None:
    assert build_settings(submission_retention_days=7).retention_policy().days == 7


# --- what is eligible --------------------------------------------------------


def test_an_old_submission_with_no_delivery_is_deleted(
    sessions: sessionmaker[Session],
) -> None:
    old = seed(sessions, received_at=CUTOFF - timedelta(days=1))

    assert sweep(sessions) == 1
    assert old not in remaining(sessions)


def test_a_recent_submission_is_kept(sessions: sessionmaker[Session]) -> None:
    recent = seed(sessions, received_at=CUTOFF + timedelta(seconds=1))

    assert sweep(sessions) == 0
    assert remaining(sessions) == {recent}


def test_a_submission_exactly_on_the_cutoff_is_kept(sessions: sessionmaker[Session]) -> None:
    """
    the cutoff is exclusive, so a boundary row survives rather than being taken early
    :param sessions: session factory on a migrated database
    """
    boundary = seed(sessions, received_at=CUTOFF)

    assert sweep(sessions) == 0
    assert remaining(sessions) == {boundary}


def test_an_old_delivered_submission_is_deleted(sessions: sessionmaker[Session]) -> None:
    """
    nothing will build a payload from a delivered submission again
    :param sessions: session factory on a migrated database
    """
    delivered = seed(
        sessions,
        received_at=CUTOFF - timedelta(days=1),
        delivery_state=DeliveryState.DELIVERED,
        attempts=1,
    )

    assert sweep(sessions) == 1
    assert delivered not in remaining(sessions)


@pytest.mark.parametrize(
    "state",
    [DeliveryState.PENDING, DeliveryState.PROCESSING, DeliveryState.FAILED],
)
def test_a_submission_a_delivery_still_needs_is_kept(
    sessions: sessionmaker[Session], state: DeliveryState
) -> None:
    """
    the payload is built from the submission at send time, so anything deliverable keeps it
    :param sessions: session factory on a migrated database
    :param state: the delivery state that must protect its submission
    """
    protected = seed(sessions, received_at=CUTOFF - timedelta(days=365), delivery_state=state)

    assert sweep(sessions) == 0
    assert remaining(sessions) == {protected}


def test_a_sweep_takes_only_what_is_eligible(sessions: sessionmaker[Session]) -> None:
    delivered = seed(
        sessions, received_at=CUTOFF - timedelta(days=1), delivery_state=DeliveryState.DELIVERED
    )
    orphan = seed(sessions, received_at=CUTOFF - timedelta(days=1))
    pending = seed(
        sessions, received_at=CUTOFF - timedelta(days=1), delivery_state=DeliveryState.PENDING
    )
    recent = seed(sessions, received_at=NOW)

    assert sweep(sessions) == 2
    assert remaining(sessions) == {pending, recent}
    assert delivered not in remaining(sessions)
    assert orphan not in remaining(sessions)


# --- what a sweep must not destroy -------------------------------------------


def test_a_deleted_submission_leaves_its_delivery_standing(
    sessions: sessionmaker[Session],
) -> None:
    """
    what this service did about a submission outlives the form content it carried
    :param sessions: session factory on a migrated database
    """
    seed(
        sessions,
        received_at=CUTOFF - timedelta(days=1),
        delivery_state=DeliveryState.DELIVERED,
        attempts=3,
    )

    sweep(sessions)

    with sessions() as session:
        delivery = session.scalars(select(models.WebhookDelivery)).one()
    assert delivery.state == DeliveryState.DELIVERED
    assert delivery.attempts == 3
    assert delivery.endpoint_id == ENDPOINT_ID
    # Unlinked rather than removed, which is the whole point of the SET NULL.
    assert delivery.submission_id is None


def test_a_deleted_submission_leaves_its_attempt_history_standing(
    sessions: sessionmaker[Session],
) -> None:
    seed(
        sessions,
        received_at=CUTOFF - timedelta(days=1),
        delivery_state=DeliveryState.DELIVERED,
        attempts=3,
    )

    sweep(sessions)

    with sessions() as session:
        attempts = list(session.scalars(select(models.DeliveryAttempt)))
    assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3]
    assert all(attempt.submission_id is None for attempt in attempts)
    assert all(attempt.delivery_id is not None for attempt in attempts)


def test_an_unlinked_delivery_is_never_claimed_by_a_worker(
    sessions: sessionmaker[Session],
) -> None:
    """
    a delivery with nothing to send must not be picked up and retried forever
    :param sessions: session factory on a migrated database
    """
    seed(
        sessions,
        received_at=CUTOFF - timedelta(days=1),
        delivery_state=DeliveryState.DELIVERED,
    )
    sweep(sessions)
    with sessions() as session:
        # Forced back into the queue behind the sweep's back, which is the only
        # way this state could ever arise.
        session.execute(
            update(models.WebhookDelivery).values(
                state=DeliveryState.PENDING, completed_at=None, next_attempt_at=CUTOFF
            )
        )
        session.commit()

    with sessions() as session:
        claimed = storage.claim_due_deliveries(session, now=NOW, lease_seconds=60, limit=10)

    assert claimed == []


def test_a_failed_delivery_keeps_what_a_replay_would_need(
    sessions: sessionmaker[Session],
) -> None:
    """
    a replay rebuilds the payload from the submission, so retention must not take it
    :param sessions: session factory on a migrated database
    """
    seed(
        sessions,
        received_at=CUTOFF - timedelta(days=365),
        delivery_state=DeliveryState.FAILED,
        attempts=5,
    )
    sweep(sessions)

    with sessions() as session:
        delivery = session.scalars(select(models.WebhookDelivery)).one()
        outcome = storage.requeue_failed_delivery(session, delivery.id, now=NOW)
        claimed = storage.claim_due_deliveries(session, now=NOW, lease_seconds=60, limit=10)
        payloads = storage.load_submissions(session, claimed)

    assert outcome.requeued is True
    assert [job.id for job in claimed] == [delivery.id]
    assert payloads[delivery.id].fields == {"email": ("dev@example.com",)}


# --- batching ----------------------------------------------------------------


def test_a_sweep_deletes_everything_eligible_across_batches(
    sessions: sessionmaker[Session],
) -> None:
    for index in range(7):
        seed(sessions, received_at=CUTOFF - timedelta(days=1 + index))

    assert sweep(sessions, batch_size=2) == 7
    assert remaining(sessions) == set()


def test_a_sweep_stops_at_its_batch_ceiling(sessions: sessionmaker[Session]) -> None:
    """
    a run against an enormous backlog has to come back rather than go on forever
    :param sessions: session factory on a migrated database
    """
    for index in range(5):
        seed(sessions, received_at=CUTOFF - timedelta(days=1 + index))

    with sessions() as session:
        removed = storage.delete_expired_submissions(
            session, before=CUTOFF, batch_size=2, max_batches=1
        )

    assert removed == 2
    assert len(remaining(sessions)) == 3


def test_counting_and_deleting_agree(sessions: sessionmaker[Session]) -> None:
    for index in range(4):
        seed(sessions, received_at=CUTOFF - timedelta(days=1 + index))
    seed(sessions, received_at=NOW, delivery_state=DeliveryState.PENDING)

    with sessions() as session:
        counted = storage.count_expired_submissions(session, before=CUTOFF)

    assert counted == 4
    assert sweep(sessions) == 4


# --- the operator command ----------------------------------------------------


def test_cleanup_refuses_to_run_with_no_retention_configured(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    seed(sessions, received_at=CUTOFF - timedelta(days=1))

    assert cli.main(["cleanup-submissions"]) == 1

    captured = capsys.readouterr()
    assert "No submission retention is configured" in captured.err
    assert captured.out == ""
    assert len(remaining(sessions)) == 1


def test_cleanup_deletes_what_the_configured_retention_releases(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")
    old = seed(sessions, received_at=datetime.now(UTC) - timedelta(days=31))
    recent = seed(sessions, received_at=datetime.now(UTC) - timedelta(days=1))

    assert cli.main(["cleanup-submissions"]) == 0

    assert "Deleted 1 submission(s)" in capsys.readouterr().out
    assert remaining(sessions) == {recent}
    assert old not in remaining(sessions)


def test_cleanup_reports_the_cutoff_it_worked_out(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")

    cli.main(["cleanup-submissions", "--dry-run"])

    output = capsys.readouterr().out
    assert "keeps submissions for 30 days" in output
    assert "eligible for deletion" in output


def test_a_dry_run_deletes_nothing(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")
    old = seed(sessions, received_at=datetime.now(UTC) - timedelta(days=31))

    assert cli.main(["cleanup-submissions", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "1 submission(s)" in output
    assert "Dry run: nothing was deleted." in output
    assert remaining(sessions) == {old}


def test_an_explicit_age_sweeps_without_any_configuration(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    """
    an operator who keeps everything must still be able to clear one old range by hand
    :param sessions: session factory on a migrated database
    :param capsys: pytest fixture capturing what the command printed
    """
    old = seed(sessions, received_at=datetime.now(UTC) - timedelta(days=400))
    recent = seed(sessions, received_at=datetime.now(UTC) - timedelta(days=10))

    assert cli.main(["cleanup-submissions", "--older-than-days", "365"]) == 0

    assert remaining(sessions) == {recent}
    assert old not in remaining(sessions)
    assert "Deleted 1 submission(s)" in capsys.readouterr().out


def test_an_explicit_age_overrides_the_configured_one(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "1")
    kept = seed(sessions, received_at=datetime.now(UTC) - timedelta(days=10))

    assert cli.main(["cleanup-submissions", "--older-than-days", "365"]) == 0

    assert remaining(sessions) == {kept}


def test_cleanup_says_so_when_there_is_nothing_to_do(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")
    seed(sessions, received_at=datetime.now(UTC))

    assert cli.main(["cleanup-submissions"]) == 0

    assert "Nothing to delete." in capsys.readouterr().out


def test_cleanup_honours_a_batch_size(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")
    for index in range(5):
        seed(sessions, received_at=datetime.now(UTC) - timedelta(days=31 + index))

    assert cli.main(["cleanup-submissions", "--batch-size", "2"]) == 0

    assert "Deleted 5 submission(s) in batches of up to 2." in capsys.readouterr().out
    assert remaining(sessions) == set()


def test_a_useless_batch_size_is_refused(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")

    assert cli.main(["cleanup-submissions", "--batch-size", "0"]) == 1

    assert "--batch-size must be at least 1." in capsys.readouterr().err


def test_cleanup_never_reports_the_database_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    a command that deletes data must not put credentials on an operator's terminal
    :param tmp_path: pytest fixture giving this test a directory of its own
    :param monkeypatch: pytest fixture used to point the CLI at an unusable database
    :param capsys: pytest fixture capturing what the command printed
    """
    monkeypatch.setenv("FORMS_DATABASE_URL", f"sqlite:///{tmp_path / 'missing' / 'forms.db'}")
    monkeypatch.setenv("FORMS_SUBMISSION_RETENTION_DAYS", "30")
    monkeypatch.setattr(cli, "Settings", IsolatedSettings)

    assert cli.main(["cleanup-submissions"]) == 1

    captured = capsys.readouterr()
    assert "The database could not be used" in captured.err
    assert captured.out == ""
