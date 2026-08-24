"""
creating endpoints through ``POST /endpoints``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import create_endpoint, open_session
from hymical_forms import models


def test_creates_an_endpoint(empty_client: TestClient) -> None:
    before = datetime.now(UTC)
    response = empty_client.post("/endpoints", json={"id": "contact-form", "name": "Contact form"})
    after = datetime.now(UTC)

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "contact-form"
    assert body["name"] == "Contact form"
    assert body["is_active"] is True
    created_at = datetime.fromisoformat(body["created_at"])
    assert before <= created_at <= after


def test_the_created_endpoint_is_persisted(empty_client: TestClient) -> None:
    create_endpoint(empty_client, "contact-form", name="Contact form")

    with open_session(empty_client) as session:
        endpoint = session.get(models.Endpoint, "contact-form")
        assert endpoint is not None
        assert endpoint.name == "Contact form"
        assert endpoint.is_active is True
        assert endpoint.created_at.tzinfo is not None


def test_a_persisted_endpoint_can_receive_submissions(empty_client: TestClient) -> None:
    create_endpoint(empty_client, "waitlist")

    response = empty_client.post("/f/waitlist", data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["endpoint_id"] == "waitlist"


def test_an_endpoint_can_be_created_inactive(empty_client: TestClient) -> None:
    body = create_endpoint(empty_client, "closed-form", is_active=False)

    assert body["is_active"] is False


def test_rejects_a_duplicate_endpoint_id(empty_client: TestClient) -> None:
    create_endpoint(empty_client, "contact-form")

    response = empty_client.post("/endpoints", json={"id": "contact-form", "name": "Another form"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "endpoint_already_exists"
    assert body["error"]["details"]["endpoint_id"] == "contact-form"


def test_a_rejected_duplicate_leaves_the_original_untouched(empty_client: TestClient) -> None:
    """
    the failed insert must not commit, and must not overwrite the existing row
    :param empty_client: test client whose app holds no endpoints
    """
    create_endpoint(empty_client, "contact-form", name="Original name")

    empty_client.post("/endpoints", json={"id": "contact-form", "name": "Replacement name"})

    with open_session(empty_client) as session:
        endpoints = list(session.scalars(select(models.Endpoint)))
        assert len(endpoints) == 1
        assert endpoints[0].name == "Original name"


@pytest.mark.parametrize("endpoint_id", ["ab", "Contact", "contact-", "con tact", "x" * 65])
def test_rejects_a_malformed_endpoint_id(empty_client: TestClient, endpoint_id: str) -> None:
    response = empty_client.post("/endpoints", json={"id": endpoint_id, "name": "A form"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_endpoint_id"


def test_a_rejected_endpoint_id_is_not_persisted(empty_client: TestClient) -> None:
    empty_client.post("/endpoints", json={"id": "Contact", "name": "A form"})

    with open_session(empty_client) as session:
        assert list(session.scalars(select(models.Endpoint))) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "contact-form"},
        {"id": "contact-form", "name": ""},
        {"id": "contact-form", "name": "x" * 201},
        {"name": "Contact form"},
    ],
)
def test_rejects_a_malformed_body(empty_client: TestClient, payload: dict[str, str]) -> None:
    response = empty_client.post("/endpoints", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_a_validation_failure_names_the_offending_field(empty_client: TestClient) -> None:
    response = empty_client.post("/endpoints", json={"id": "contact-form", "name": ""})

    fields = response.json()["error"]["details"]["fields"]
    assert [field["field"] for field in fields] == ["name"]
