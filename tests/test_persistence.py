"""
what reaches the database when a submission is accepted, and what does not
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from conftest import URLENCODED_HEADERS, ClientFactory, create_endpoint, open_session
from hymical_forms import models

ENDPOINT = "/f/contact-form"


def stored_submissions(client: TestClient) -> list[models.Submission]:
    """
    read every persisted submission behind a client
    :param client: the client whose application database should be inspected
    :returns: the submission rows, detached from their session
    """
    with open_session(client) as session:
        return list(session.scalars(select(models.Submission)))


def test_an_accepted_submission_is_persisted(client: TestClient) -> None:
    response = client.post(ENDPOINT, data={"email": "dev@example.com", "message": "hello"})

    assert response.status_code == 202
    submissions = stored_submissions(client)
    assert len(submissions) == 1
    assert submissions[0].endpoint_id == "contact-form"


def test_the_returned_id_matches_the_persisted_row(client: TestClient) -> None:
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert stored_submissions(client)[0].id == response.json()["submission_id"]


def test_the_returned_timestamp_matches_the_persisted_row(client: TestClient) -> None:
    before = datetime.now(UTC)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    after = datetime.now(UTC)

    received_at = stored_submissions(client)[0].received_at
    assert received_at.tzinfo is not None
    assert before <= received_at <= after
    assert received_at == datetime.fromisoformat(response.json()["received_at"])


def test_repeated_field_values_survive_persistence(client: TestClient) -> None:
    """
    a checkbox group must come back out with every value, in the order it arrived
    :param client: test client whose app already holds the default endpoint
    """
    response = client.post(
        ENDPOINT, data={"topic": ["billing", "api", "docs"], "email": "dev@example.com"}
    )

    assert response.json()["field_count"] == 4
    submission = stored_submissions(client)[0].to_domain()
    assert submission.fields == {
        "topic": ("billing", "api", "docs"),
        "email": ("dev@example.com",),
    }
    assert submission.field_count == 4


def test_field_order_survives_persistence(client: TestClient) -> None:
    """
    JSON object key order is preserved, so a form's fields keep their order
    :param client: test client whose app already holds the default endpoint
    """
    client.post(ENDPOINT, content=b"zebra=1&apple=2&mango=3", headers=URLENCODED_HEADERS)

    assert list(stored_submissions(client)[0].fields) == ["zebra", "apple", "mango"]


def test_a_blank_value_survives_persistence(client: TestClient) -> None:
    client.post(ENDPOINT, content=b"nickname=", headers=URLENCODED_HEADERS)

    assert stored_submissions(client)[0].fields == {"nickname": [""]}


def test_submissions_are_persisted_against_their_own_endpoint(empty_client: TestClient) -> None:
    create_endpoint(empty_client, "contact-form")
    create_endpoint(empty_client, "waitlist")

    empty_client.post("/f/contact-form", data={"email": "a@example.com"})
    empty_client.post("/f/waitlist", data={"email": "b@example.com"})

    stored = {row.endpoint_id for row in stored_submissions(empty_client)}
    assert stored == {"contact-form", "waitlist"}


def test_rejects_a_submission_to_an_unknown_endpoint(empty_client: TestClient) -> None:
    response = empty_client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "endpoint_not_found"
    assert body["error"]["details"]["endpoint_id"] == "contact-form"
    assert stored_submissions(empty_client) == []


def test_rejects_a_submission_to_an_inactive_endpoint(empty_client: TestClient) -> None:
    create_endpoint(empty_client, "contact-form", is_active=False)

    response = empty_client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "endpoint_inactive"
    assert stored_submissions(empty_client) == []


@pytest.mark.parametrize(
    ("description", "kwargs", "expected_status"),
    [
        ("empty submission", {"content": b"", "headers": URLENCODED_HEADERS}, 422),
        (
            "malformed multipart",
            {"content": b"--x\r\nnope", "headers": {"content-type": "multipart/form-data"}},
            400,
        ),
        ("unsupported content type", {"json": {"email": "a@b.co"}}, 415),
    ],
)
def test_an_invalid_submission_is_not_persisted(
    client: TestClient, description: str, kwargs: dict[str, object], expected_status: int
) -> None:
    response = client.post(ENDPOINT, **kwargs)  # type: ignore[arg-type]

    assert response.status_code == expected_status, description
    assert stored_submissions(client) == []


def test_an_over_limit_submission_is_not_persisted(make_client: ClientFactory) -> None:
    client = make_client(max_fields=2)

    response = client.post(ENDPOINT, data={"a": "1", "b": "2", "c": "3"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "too_many_fields"
    assert stored_submissions(client) == []


def test_an_oversized_body_is_not_persisted(make_client: ClientFactory) -> None:
    client = make_client(max_body_bytes=64)

    response = client.post(ENDPOINT, content=b"note=" + b"x" * 200, headers=URLENCODED_HEADERS)

    assert response.status_code == 413
    assert stored_submissions(client) == []


def test_a_storage_failure_is_not_acknowledged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    a commit that fails must produce an error, never a 202 for a submission that vanished
    :param client: test client whose app already holds the default endpoint
    :param monkeypatch: pytest fixture used to break the commit for one request
    """

    def failing_commit(self: Session) -> None:
        raise OperationalError(
            "INSERT INTO submissions (id, endpoint_id) VALUES (?, ?)",
            {},
            Exception("server closed the connection unexpectedly"),
        )

    monkeypatch.setattr(Session, "commit", failing_commit)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    monkeypatch.undo()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert stored_submissions(client) == []


def test_a_storage_failure_does_not_leak_database_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    the error envelope must carry no SQL, table names or driver messages
    :param client: test client whose app already holds the default endpoint
    :param monkeypatch: pytest fixture used to break the commit for one request
    """

    def failing_commit(self: Session) -> None:
        raise OperationalError(
            "INSERT INTO submissions (id, endpoint_id) VALUES (?, ?)",
            {},
            Exception("server closed the connection unexpectedly"),
        )

    monkeypatch.setattr(Session, "commit", failing_commit)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    monkeypatch.undo()

    text = response.text
    assert "INSERT" not in text
    assert "server closed the connection" not in text
    assert "OperationalError" not in text
