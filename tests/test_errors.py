"""
the shared error envelope
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import URLENCODED_HEADERS, build_settings
from hymical_forms.app import create_app

ENDPOINT = "/f/contact-form"


def test_errors_share_one_envelope(client: TestClient) -> None:
    response = client.post(ENDPOINT, json={"email": "dev@example.com"})

    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert set(error) <= {"code", "message", "details"}
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)


def test_details_are_omitted_when_there_is_nothing_to_add(client: TestClient) -> None:
    response = client.post(ENDPOINT, content=b"", headers=URLENCODED_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "empty_submission", "message": "Submission contains no fields."}
    }


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain", "application/octet-stream"],
)
def test_rejects_unsupported_content_types(client: TestClient, content_type: str) -> None:
    response = client.post(
        ENDPOINT, content=b"email=a%40b.co", headers={"content-type": content_type}
    )

    assert response.status_code == 415
    body = response.json()
    assert body["error"]["code"] == "unsupported_media_type"
    assert body["error"]["details"]["received"] == content_type


def test_rejects_a_request_without_a_content_type(client: TestClient) -> None:
    response = client.post(ENDPOINT, content=b"email=a%40b.co", headers={"content-type": ""})

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_rejects_multipart_without_a_boundary(client: TestClient) -> None:
    response = client.post(
        ENDPOINT, content=b"--x\r\nnope", headers={"content-type": "multipart/form-data"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_form_body"


def test_rejects_a_multipart_body_that_does_not_match_its_boundary(client: TestClient) -> None:
    response = client.post(
        ENDPOINT,
        content=b"email=a%40b.co",
        headers={"content-type": "multipart/form-data; boundary=hymical"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_form_body"


def test_rejects_file_uploads(client: TestClient) -> None:
    """
    file handling is out of scope, so a file part is refused rather than ignored
    :param client: test client for an app on default settings
    """
    response = client.post(
        ENDPOINT,
        data={"email": "dev@example.com"},
        files={"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "file_upload_not_supported"
    assert body["error"]["details"]["field"] == "resume"


def test_unknown_paths_use_the_envelope(client: TestClient) -> None:
    response = client.post("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_wrong_methods_use_the_envelope(client: TestClient) -> None:
    response = client.get(ENDPOINT)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_unexpected_errors_do_not_leak_internals() -> None:
    app = create_app(build_settings())

    @app.get("/boom")
    async def boom() -> None:
        """
        raise an error carrying a secret, to prove the handler does not relay it
        """
        raise RuntimeError("connection string: postgres://user:pa55w0rd@db/forms")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "The request could not be processed."}
    }
    assert "pa55w0rd" not in response.text
