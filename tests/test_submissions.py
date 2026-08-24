"""
accepting form submissions
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from conftest import URLENCODED_HEADERS

ENDPOINT = "/f/contact-form"


def test_accepts_a_urlencoded_submission(client: TestClient) -> None:
    response = client.post(ENDPOINT, data={"email": "dev@example.com", "message": "hello"})

    assert response.status_code == 202
    body = response.json()
    assert body["endpoint_id"] == "contact-form"
    assert body["field_count"] == 2


def test_accepts_a_multipart_submission(client: TestClient) -> None:
    """
    browsers send ``enctype="multipart/form-data"`` forms, so text parts are accepted
    :param client: test client for an app on default settings
    """
    boundary = "hymicalboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="email"\r\n\r\n'
        "dev@example.com\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    response = client.post(
        ENDPOINT,
        content=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 202
    assert response.json()["field_count"] == 1


def test_generates_submission_metadata(client: TestClient) -> None:
    before = datetime.now(UTC)
    response = client.post(ENDPOINT, data={"email": "dev@example.com"})
    after = datetime.now(UTC)

    body = response.json()
    assert body["submission_id"].startswith("sub_")
    received_at = datetime.fromisoformat(body["received_at"])
    assert received_at.tzinfo is not None
    assert before <= received_at <= after


def test_each_submission_gets_a_distinct_id(client: TestClient) -> None:
    ids = {
        client.post(ENDPOINT, data={"email": "dev@example.com"}).json()["submission_id"]
        for _ in range(5)
    }

    assert len(ids) == 5


def test_repeated_field_names_are_all_counted(client: TestClient) -> None:
    """
    checkbox groups submit one name several times, so no value may be dropped
    :param client: test client for an app on default settings
    """
    response = client.post(ENDPOINT, data={"topic": ["billing", "api", "docs"], "email": "a@b.co"})

    assert response.status_code == 202
    assert response.json()["field_count"] == 4


def test_accepts_a_field_with_an_empty_value(client: TestClient) -> None:
    """
    an optional text input that the user left blank is still a submitted field
    :param client: test client for an app on default settings
    """
    response = client.post(ENDPOINT, content=b"nickname=", headers=URLENCODED_HEADERS)

    assert response.status_code == 202
    assert response.json()["field_count"] == 1


def test_accepts_non_ascii_values(client: TestClient) -> None:
    response = client.post(ENDPOINT, data={"name": "Zoë", "note": "naïve café"})

    assert response.status_code == 202
    assert response.json()["field_count"] == 2


def test_content_type_parameters_and_casing_are_ignored(client: TestClient) -> None:
    response = client.post(
        ENDPOINT,
        content=b"email=dev%40example.com",
        headers={"content-type": "APPLICATION/X-WWW-Form-Urlencoded; charset=UTF-8"},
    )

    assert response.status_code == 202


def test_does_not_echo_submitted_values(client: TestClient) -> None:
    """
    the acknowledgement is metadata only, so user input is not reflected back
    :param client: test client for an app on default settings
    """
    response = client.post(ENDPOINT, data={"secret": "hunter2"})

    assert "hunter2" not in response.text
    assert set(response.json()) == {
        "submission_id",
        "endpoint_id",
        "received_at",
        "field_count",
        "idempotent_replay",
        "delivery",
    }
