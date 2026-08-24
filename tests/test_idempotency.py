"""
idempotent ingestion: safe retries, key reuse, and the race between them
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import ClientFactory, create_endpoint, open_session
from hymical_forms import models, storage
from hymical_forms.ingestion import payload_fingerprint

ENDPOINT = "/f/contact-form"
KEY = "b8f1c2d4e5a67890b8f1c2d4e5a67890"
OTHER_KEY = "0123456789abcdef0123456789abcdef"

PAYLOAD = {"email": "dev@example.com", "message": "hello"}


def submit(
    client: TestClient,
    data: dict[str, Any] | None = None,
    *,
    key: str | None = KEY,
    path: str = ENDPOINT,
) -> Any:
    """
    post a form, optionally carrying an idempotency key
    :param client: the client to post through
    :param data: form fields to send, defaulting to a fixed payload
    :param key: idempotency key to send, or None to omit the header
    :param path: the ingestion path to post to
    :returns: the HTTP response
    """
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(path, data=PAYLOAD if data is None else data, headers=headers)


def stored(client: TestClient) -> list[models.Submission]:
    """
    read every persisted submission behind a client
    :param client: the client whose application database should be inspected
    :returns: the submission rows
    """
    with open_session(client) as session:
        return list(session.scalars(select(models.Submission)))


# --- without a key: nothing changes -----------------------------------------


def test_without_a_key_identical_requests_create_two_submissions(client: TestClient) -> None:
    first = submit(client, key=None)
    second = submit(client, key=None)

    assert first.status_code == second.status_code == 202
    assert first.json()["submission_id"] != second.json()["submission_id"]
    assert len(stored(client)) == 2


def test_without_a_key_the_response_is_never_a_replay(client: TestClient) -> None:
    assert submit(client, key=None).json()["idempotent_replay"] is False


def test_without_a_key_no_idempotency_identity_is_stored(client: TestClient) -> None:
    submit(client, key=None)

    row = stored(client)[0]
    assert row.idempotency_key is None
    assert row.payload_fingerprint is None


# --- first keyed submission --------------------------------------------------


def test_a_keyed_submission_stores_its_idempotency_identity(client: TestClient) -> None:
    response = submit(client)

    assert response.status_code == 202
    assert response.json()["idempotent_replay"] is False
    row = stored(client)[0]
    assert row.idempotency_key == KEY
    assert row.payload_fingerprint == payload_fingerprint(row.to_domain().fields)


# --- replay ------------------------------------------------------------------


def test_replaying_a_key_returns_the_original_submission(client: TestClient) -> None:
    first = submit(client).json()
    second = submit(client).json()

    assert len(stored(client)) == 1
    assert second["submission_id"] == first["submission_id"]
    assert second["received_at"] == first["received_at"]
    assert second["field_count"] == first["field_count"]
    assert second["endpoint_id"] == first["endpoint_id"]


def test_a_replay_is_flagged_and_still_accepted(client: TestClient) -> None:
    submit(client)
    response = submit(client)

    assert response.status_code == 202
    assert response.json()["idempotent_replay"] is True


def test_many_replays_still_leave_one_submission(client: TestClient) -> None:
    ids = {submit(client).json()["submission_id"] for _ in range(5)}

    assert len(ids) == 1
    assert len(stored(client)) == 1


# --- conflict ----------------------------------------------------------------


def test_reusing_a_key_for_different_content_conflicts(client: TestClient) -> None:
    submit(client)

    response = submit(client, {"email": "dev@example.com", "message": "different"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "idempotency_conflict"
    assert body["error"]["details"]["idempotency_key"] == KEY


def test_a_conflict_leaves_the_original_row_untouched(client: TestClient) -> None:
    original = submit(client).json()

    submit(client, {"email": "attacker@example.com", "message": "overwritten"})

    rows = stored(client)
    assert len(rows) == 1
    assert rows[0].id == original["submission_id"]
    assert rows[0].fields == {"email": ["dev@example.com"], "message": ["hello"]}


def test_a_conflict_does_not_describe_the_stored_payload(client: TestClient) -> None:
    """
    the key is endpoint-scoped and unauthenticated, so a clash must not read content back
    :param client: test client whose app already holds the default endpoint
    """
    submit(client, {"secret": "original-value"})

    response = submit(client, {"secret": "guess"})

    assert response.status_code == 409
    assert "original-value" not in response.text


# --- scope -------------------------------------------------------------------


def test_a_key_is_scoped_to_one_endpoint(empty_client: TestClient) -> None:
    create_endpoint(empty_client, "contact-form")
    create_endpoint(empty_client, "waitlist")

    first = submit(empty_client, path="/f/contact-form")
    second = submit(empty_client, path="/f/waitlist")

    assert first.status_code == second.status_code == 202
    assert second.json()["idempotent_replay"] is False
    assert first.json()["submission_id"] != second.json()["submission_id"]
    assert {row.endpoint_id for row in stored(empty_client)} == {"contact-form", "waitlist"}


# --- what counts as the same payload -----------------------------------------


def test_repeated_values_replay_when_unchanged(client: TestClient) -> None:
    payload = {"topic": ["billing", "api"], "email": "dev@example.com"}
    first = submit(client, payload)

    second = submit(client, payload)

    assert second.status_code == 202
    assert second.json()["submission_id"] == first.json()["submission_id"]


@pytest.mark.parametrize(
    ("description", "changed"),
    [
        ("a value differs", {"topic": ["billing", "docs"]}),
        ("a value is dropped", {"topic": ["billing"]}),
        ("a value is added", {"topic": ["billing", "api", "docs"]}),
        ("the values are reordered", {"topic": ["api", "billing"]}),
        ("a value is duplicated", {"topic": ["billing", "api", "api"]}),
    ],
)
def test_changing_repeated_values_conflicts(
    client: TestClient, description: str, changed: dict[str, list[str]]
) -> None:
    submit(client, {"topic": ["billing", "api"]})

    response = submit(client, changed)

    assert response.status_code == 409, description
    assert len(stored(client)) == 1


def test_reordering_fields_conflicts(client: TestClient) -> None:
    """
    field order is preserved through storage, so it is part of what a payload is
    :param client: test client whose app already holds the default endpoint
    """
    headers = {"Idempotency-Key": KEY, "content-type": "application/x-www-form-urlencoded"}
    client.post(ENDPOINT, content=b"a=1&b=2", headers=headers)

    response = client.post(ENDPOINT, content=b"b=2&a=1", headers=headers)

    assert response.status_code == 409
    assert len(stored(client)) == 1


def test_the_fingerprint_ignores_generated_metadata(client: TestClient) -> None:
    """
    two submissions of the same content agree on a fingerprint despite differing ids
    :param client: test client whose app already holds the default endpoint
    """
    submit(client, key=None)
    submit(client, key=None)

    first, second = stored(client)
    assert first.id != second.id
    assert payload_fingerprint(first.to_domain().fields) == payload_fingerprint(
        second.to_domain().fields
    )


# --- key syntax --------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "key"),
    [
        ("empty", ""),
        ("too short", "abc"),
        ("one below the minimum", "a" * 15),
        ("too long", "a" * 256),
        ("contains a space", "abcdef ghijklmnop"),
        ("contains a tab", "abcdef\tghijklmnop"),
        ("contains a newline", "abcdefghijklmnop\n"),
    ],
)
def test_rejects_an_unusable_key(client: TestClient, description: str, key: str) -> None:
    response = client.post(ENDPOINT, data=PAYLOAD, headers={"Idempotency-Key": key})

    assert response.status_code == 400, description
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert stored(client) == []


def test_rejects_a_non_ascii_key(client: TestClient) -> None:
    """
    header values are bytes, so non-ASCII arrives latin-1 encoded rather than as text
    :param client: test client whose app already holds the default endpoint
    """
    response = client.post(
        ENDPOINT,
        data=PAYLOAD,
        headers={b"Idempotency-Key": "kaffee-fur-alle-éééé".encode("latin-1")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert stored(client) == []


@pytest.mark.parametrize(
    ("description", "key"),
    [
        ("a uuid", "550e8400-e29b-41d4-a716-446655440000"),
        ("hex", "b8f1c2d4e5a67890b8f1c2d4e5a67890"),
        ("base64url", "abcDEF-_0123456789xyz"),
        ("base64 with padding", "YWJjZGVmZ2hpamtsbW5vcA=="),
        ("at the minimum length", "a" * 16),
        ("at the maximum length", "a" * 255),
    ],
)
def test_accepts_a_practical_key(client: TestClient, description: str, key: str) -> None:
    response = client.post(ENDPOINT, data=PAYLOAD, headers={"Idempotency-Key": key})

    assert response.status_code == 202, description


# --- the race ----------------------------------------------------------------


def file_backed_client(make_client: ClientFactory, tmp_path: Path) -> TestClient:
    """
    build a client whose database is a real file, so each session gets its own connection
    :param make_client: factory for clients bound to a configured app
    :param tmp_path: pytest-provided directory to hold the database file
    :returns: a test client that can serve genuinely concurrent writes
    """
    # The in-memory database used elsewhere is pinned to a single shared
    # connection, which cannot express two writers racing.
    return make_client(database_url=f"sqlite:///{tmp_path.as_posix()}/forms.db")


def fire_together(calls: list[Callable[[], Any]]) -> list[Any]:
    """
    run callables from separate threads, released at the same moment
    :param calls: the callables to run, one per thread
    :returns: each callable's result, in the order given
    """
    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], Any]) -> Any:
        barrier.wait()
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [future.result() for future in [pool.submit(run, call) for call in calls]]


def test_concurrent_identical_submissions_store_one_row(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    """
    the losers of the insert race must resolve to the winner's row, not to duplicates
    :param make_client: factory for clients bound to a configured app
    :param tmp_path: pytest-provided directory to hold the database file
    """
    client = file_backed_client(make_client, tmp_path)

    responses = fire_together([lambda: submit(client) for _ in range(6)])

    assert [r.status_code for r in responses] == [202] * 6
    assert len({r.json()["submission_id"] for r in responses}) == 1
    assert len(stored(client)) == 1
    assert sum(r.json()["idempotent_replay"] for r in responses) == 5


def test_concurrent_conflicting_submissions_store_one_row(
    make_client: ClientFactory, tmp_path: Path
) -> None:
    """
    exactly one racer wins the key; the rest must be refused rather than duplicated
    :param make_client: factory for clients bound to a configured app
    :param tmp_path: pytest-provided directory to hold the database file
    """
    client = file_backed_client(make_client, tmp_path)

    responses = fire_together(
        [(lambda n=n: submit(client, {"message": f"payload-{n}"})) for n in range(6)]  # type: ignore[misc]
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202] + [409] * 5
    assert len(stored(client)) == 1


def test_losing_the_race_replays_the_winner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    drive the reconciliation branch deterministically, with a real constraint violation
    :param client: test client whose app already holds the default endpoint
    :param monkeypatch: pytest fixture used to blind the pre-check lookup once
    """
    # Only the optimistic lookup is blinded, and only for the attempt that races.
    # The unique violation, the rollback and the re-read are all real, which is
    # exactly what a request that lost the race actually experiences.
    original = submit(client).json()
    real_lookup = storage.find_by_idempotency_key
    calls = {"n": 0}

    def blind_first_lookup(session: Session, endpoint_id: str, key: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_lookup(session, endpoint_id, key)

    monkeypatch.setattr(storage, "find_by_idempotency_key", blind_first_lookup)
    response = submit(client)
    monkeypatch.undo()

    assert calls["n"] == 2, "the insert should have failed and forced a second lookup"
    assert response.status_code == 202
    assert response.json()["submission_id"] == original["submission_id"]
    assert response.json()["idempotent_replay"] is True
    assert len(stored(client)) == 1


def test_losing_the_race_with_different_content_conflicts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    a racer whose payload differs must be refused once it sees the winner's row
    :param client: test client whose app already holds the default endpoint
    :param monkeypatch: pytest fixture used to blind the pre-check lookup once
    """
    submit(client)
    real_lookup = storage.find_by_idempotency_key
    calls = {"n": 0}

    def blind_first_lookup(session: Session, endpoint_id: str, key: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_lookup(session, endpoint_id, key)

    monkeypatch.setattr(storage, "find_by_idempotency_key", blind_first_lookup)
    response = submit(client, {"message": "different"})
    monkeypatch.undo()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert len(stored(client)) == 1


def test_an_unexplained_integrity_error_is_not_reported_as_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    a constraint violation we cannot attribute to the key must not become a 202
    :param client: test client whose app already holds the default endpoint
    :param monkeypatch: pytest fixture used to blind the lookup permanently
    """
    submit(client)
    monkeypatch.setattr(storage, "find_by_idempotency_key", lambda *args: None)

    response = submit(client)
    monkeypatch.undo()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert len(stored(client)) == 1
