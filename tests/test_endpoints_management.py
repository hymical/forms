"""
reading and changing endpoints through the management API

``POST /endpoints`` is covered by ``test_endpoints_api.py``; this module is about
the routes interval 8 added around it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import (
    ClientFactory,
    bearer,
    create_endpoint,
    management_key,
    open_session,
    work_once,
)
from hymical_forms import models
from webhook_server import WebhookRecorder

MANAGEMENT_ROUTES = [
    ("GET", "/endpoints", None),
    ("GET", "/endpoints/contact-form", None),
    ("PATCH", "/endpoints/contact-form", {"name": "Renamed"}),
]


def seed(client: TestClient, count: int, *, prefix: str = "form") -> list[str]:
    """
    register several endpoints in a known order
    :param client: the client whose application should hold them
    :param count: how many to register
    :param prefix: the identifier prefix each one is built from
    :returns: the identifiers, oldest first
    """
    return [create_endpoint(client, f"{prefix}-{index}")["id"] for index in range(count)]


def walk(client: TestClient, *, limit: int) -> list[str]:
    """
    follow the cursor to the end and collect every endpoint identifier seen
    :param client: the client to page through
    :param limit: the page size to ask for on every request
    :returns: the identifiers in the order the API returned them
    """
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(100):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/endpoints", params=params).json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("the cursor never ran out")


# --- authentication ----------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT_ROUTES)
def test_a_management_endpoint_route_refuses_an_unauthenticated_request(
    make_client: ClientFactory, method: str, path: str, body: dict[str, Any] | None
) -> None:
    client = make_client(authenticate=False)

    response = client.request(method, path, json=body)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT_ROUTES)
def test_a_management_endpoint_route_refuses_an_unusable_key(
    make_client: ClientFactory, method: str, path: str, body: dict[str, Any] | None
) -> None:
    client = make_client(authenticate=False)

    response = client.request(method, path, json=body, headers=bearer("hym_live_nope"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_a_refused_patch_changes_nothing(make_client: ClientFactory) -> None:
    client = make_client(authenticate=False)

    client.patch("/endpoints/contact-form", json={"name": "Renamed"})

    with open_session(client) as session:
        endpoint = session.get(models.Endpoint, "contact-form")
        assert endpoint is not None
        assert endpoint.name == "Contact form"


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT_ROUTES)
def test_a_valid_key_reaches_every_management_endpoint_route(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method, path, json=body)

    assert response.status_code == 200, response.text


# --- listing -----------------------------------------------------------------


def test_listing_returns_the_registered_endpoints(client: TestClient) -> None:
    create_endpoint(client, "waitlist", name="Waitlist")

    body = client.get("/endpoints").json()

    assert {item["id"] for item in body["items"]} == {"contact-form", "waitlist"}
    assert body["next_cursor"] is None


def test_listing_an_empty_service_returns_an_empty_page(empty_client: TestClient) -> None:
    body = empty_client.get("/endpoints").json()

    assert body == {"items": [], "next_cursor": None}


def test_a_listed_endpoint_carries_its_configuration(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, "contact-form", name="Contact form", webhook_url=webhook.url)

    item = client.get("/endpoints").json()["items"][0]

    assert item["id"] == "contact-form"
    assert item["name"] == "Contact form"
    assert item["is_active"] is True
    assert item["webhook_url"] == webhook.url
    assert isinstance(item["created_at"], str)


def test_listing_is_ordered_newest_first(empty_client: TestClient) -> None:
    seed(empty_client, 4)

    listed = [item["id"] for item in empty_client.get("/endpoints").json()["items"]]

    assert listed == ["form-3", "form-2", "form-1", "form-0"]


def test_paging_visits_every_endpoint_exactly_once(empty_client: TestClient) -> None:
    created = seed(empty_client, 7)

    seen = walk(empty_client, limit=2)

    assert seen == list(reversed(created))


def test_a_page_is_bounded_by_the_requested_limit(empty_client: TestClient) -> None:
    seed(empty_client, 5)

    body = empty_client.get("/endpoints", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    assert body["next_cursor"] == body["items"][-1]["id"]


def test_the_same_page_request_answers_identically(empty_client: TestClient) -> None:
    """
    a page has to be reproducible, or a cursor walk cannot be trusted
    :param empty_client: test client whose app holds no endpoints
    """
    seed(empty_client, 5)

    first = empty_client.get("/endpoints", params={"limit": 3}).json()
    again = empty_client.get("/endpoints", params={"limit": 3}).json()

    assert first == again


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_an_unusable_page_size_is_refused(empty_client: TestClient, limit: int) -> None:
    response = empty_client.get("/endpoints", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_an_unknown_cursor_is_refused(empty_client: TestClient) -> None:
    seed(empty_client, 2)

    response = empty_client.get("/endpoints", params={"cursor": "no-such-endpoint"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_listing_never_carries_a_webhook_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    created = create_endpoint(client, "contact-form", webhook_url=webhook.url)

    response = client.get("/endpoints")

    assert created["webhook_secret"] is not None
    assert created["webhook_secret"] not in response.text
    assert "webhook_secret" not in response.json()["items"][0]


# --- detail ------------------------------------------------------------------


def test_detail_describes_one_endpoint(client: TestClient) -> None:
    body = client.get("/endpoints/contact-form").json()

    assert body["id"] == "contact-form"
    assert body["name"] == "Contact form"
    assert body["is_active"] is True
    assert body["webhook_url"] is None


def test_detail_never_carries_a_webhook_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    created = create_endpoint(client, "contact-form", webhook_url=webhook.url)

    response = client.get("/endpoints/contact-form")

    assert created["webhook_secret"] not in response.text
    assert "webhook_secret" not in response.json()


def test_detail_of_an_unknown_endpoint_is_a_404(empty_client: TestClient) -> None:
    response = empty_client.get("/endpoints/nothing-here")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "endpoint_not_found"
    assert body["error"]["details"]["endpoint_id"] == "nothing-here"


# --- updating ----------------------------------------------------------------


def test_the_name_can_be_changed(client: TestClient) -> None:
    response = client.patch("/endpoints/contact-form", json={"name": "Support form"})

    assert response.status_code == 200
    assert response.json()["name"] == "Support form"
    assert client.get("/endpoints/contact-form").json()["name"] == "Support form"


def test_the_active_state_can_be_changed(client: TestClient) -> None:
    response = client.patch("/endpoints/contact-form", json={"is_active": False})

    assert response.json()["is_active"] is False
    assert client.get("/endpoints/contact-form").json()["is_active"] is False


def test_an_omitted_field_is_left_alone(client: TestClient) -> None:
    client.patch("/endpoints/contact-form", json={"name": "Support form"})

    body = client.patch("/endpoints/contact-form", json={"is_active": False}).json()

    assert body["name"] == "Support form"
    assert body["is_active"] is False


def test_an_empty_patch_changes_nothing(client: TestClient) -> None:
    before = client.get("/endpoints/contact-form").json()

    response = client.patch("/endpoints/contact-form", json={})

    assert response.status_code == 200
    assert {key: value for key, value in response.json().items() if key in before} == before


def test_the_endpoint_id_cannot_be_changed(client: TestClient) -> None:
    """
    the ID is the primary key and sits in the action URL of every deployed form
    :param client: test client whose app holds the default endpoint
    """
    response = client.patch("/endpoints/contact-form", json={"id": "renamed-form"})

    assert response.status_code == 200
    assert response.json()["id"] == "contact-form"
    with open_session(client) as session:
        assert session.get(models.Endpoint, "renamed-form") is None
        assert session.get(models.Endpoint, "contact-form") is not None


def test_patching_an_unknown_endpoint_is_a_404(empty_client: TestClient) -> None:
    response = empty_client.patch("/endpoints/nothing-here", json={"name": "Whatever"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "endpoint_not_found"


@pytest.mark.parametrize("payload", [{"name": ""}, {"name": "x" * 201}, {"is_active": "maybe"}])
def test_an_unusable_patch_body_is_refused(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.patch("/endpoints/contact-form", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


# --- disabling and re-enabling -----------------------------------------------


def test_disabling_stops_submissions_immediately(client: TestClient) -> None:
    client.patch("/endpoints/contact-form", json={"is_active": False})

    response = client.post("/f/contact-form", data={"email": "dev@example.com"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "endpoint_inactive"


def test_a_submission_to_a_disabled_endpoint_persists_nothing(client: TestClient) -> None:
    client.patch("/endpoints/contact-form", json={"is_active": False})

    client.post("/f/contact-form", data={"email": "dev@example.com"})

    with open_session(client) as session:
        assert session.query(models.Submission).count() == 0


def test_re_enabling_restores_acceptance(client: TestClient) -> None:
    client.patch("/endpoints/contact-form", json={"is_active": False})

    client.patch("/endpoints/contact-form", json={"is_active": True})

    response = client.post("/f/contact-form", data={"email": "dev@example.com"})
    assert response.status_code == 202


# --- webhook reconfiguration --------------------------------------------------


def test_adding_a_webhook_generates_a_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(allow_private_webhook_targets=True)

    body = client.patch("/endpoints/contact-form", json={"webhook_url": webhook.url}).json()

    assert body["webhook_url"] == webhook.url
    assert body["webhook_secret"] is not None
    assert body["webhook_secret"].startswith("whsec_")


def test_the_generated_secret_is_the_one_deliveries_are_signed_with(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(allow_private_webhook_targets=True)
    secret = client.patch("/endpoints/contact-form", json={"webhook_url": webhook.url}).json()[
        "webhook_secret"
    ]

    client.post("/f/contact-form", data={"email": "dev@example.com"})
    work_once(client)

    delivered = webhook.received[0]
    expected = hmac.new(secret.encode("utf-8"), delivered.body, hashlib.sha256).hexdigest()
    assert delivered.headers["hymical-signature"] == f"v1={expected}"


def test_an_unchanged_destination_keeps_its_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    resending the same URL must not silently invalidate a receiver's secret
    :param make_client: factory for clients bound to a configured app
    :param webhook: a local server standing in for the receiver
    """
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    created = create_endpoint(client, "contact-form", webhook_url=webhook.url)

    body = client.patch(
        "/endpoints/contact-form", json={"name": "Renamed", "webhook_url": webhook.url}
    ).json()

    assert body["webhook_secret"] is None
    with open_session(client) as session:
        endpoint = session.get(models.Endpoint, "contact-form")
        assert endpoint is not None
        assert endpoint.webhook_secret == created["webhook_secret"]


def test_changing_the_destination_generates_a_new_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    created = create_endpoint(client, "contact-form", webhook_url=webhook.url)

    body = client.patch(
        "/endpoints/contact-form", json={"webhook_url": webhook.url + "/moved"}
    ).json()

    assert body["webhook_secret"] is not None
    assert body["webhook_secret"] != created["webhook_secret"]
    with open_session(client) as session:
        endpoint = session.get(models.Endpoint, "contact-form")
        assert endpoint is not None
        assert endpoint.webhook_secret == body["webhook_secret"]


def test_removing_the_webhook_removes_the_secret_too(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, "contact-form", webhook_url=webhook.url)

    body = client.patch("/endpoints/contact-form", json={"webhook_url": None}).json()

    assert body["webhook_url"] is None
    assert body["webhook_secret"] is None
    with open_session(client) as session:
        endpoint = session.get(models.Endpoint, "contact-form")
        assert endpoint is not None
        assert endpoint.webhook_secret is None


def test_a_submission_after_removal_queues_nothing(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, "contact-form", webhook_url=webhook.url)
    client.patch("/endpoints/contact-form", json={"webhook_url": None})

    response = client.post("/f/contact-form", data={"email": "dev@example.com"})

    assert response.json()["delivery"]["queued"] is False
    assert work_once(client) == 0


def test_an_unusable_destination_is_refused(client: TestClient) -> None:
    response = client.patch(
        "/endpoints/contact-form", json={"webhook_url": "http://127.0.0.1/hook"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_webhook_url"


def test_a_refused_destination_leaves_the_endpoint_alone(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=False)
    create_endpoint(client, "contact-form", name="Contact form")

    client.patch("/endpoints/contact-form", json={"name": "Renamed", "webhook_url": "ftp://x/y"})

    with open_session(client) as session:
        endpoint = session.get(models.Endpoint, "contact-form")
        assert endpoint is not None
        assert endpoint.name == "Contact form"
        assert endpoint.webhook_url is None


def test_a_queued_delivery_keeps_the_destination_it_snapshotted(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    changing configuration must not redirect work that is already owed
    :param make_client: factory for clients bound to a configured app
    :param webhook: a local server standing in for the original receiver
    """
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    created = create_endpoint(client, "contact-form", webhook_url=webhook.url)
    client.post("/f/contact-form", data={"email": "dev@example.com"})

    client.patch("/endpoints/contact-form", json={"webhook_url": webhook.url + "/elsewhere"})

    with open_session(client) as session:
        delivery = session.query(models.WebhookDelivery).one()
        assert delivery.destination_url == webhook.url
        assert delivery.signing_secret == created["webhook_secret"]


def test_a_patch_never_logs_the_generated_secret(
    make_client: ClientFactory, webhook: WebhookRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(allow_private_webhook_targets=True)

    with caplog.at_level(logging.DEBUG):
        secret = client.patch("/endpoints/contact-form", json={"webhook_url": webhook.url}).json()[
            "webhook_secret"
        ]

    assert caplog.text != ""
    assert secret not in caplog.text


def test_a_patch_does_not_log_the_management_credential(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    key = management_key(client)

    with caplog.at_level(logging.DEBUG):
        client.patch("/endpoints/contact-form", json={"name": "Renamed"})

    assert key not in caplog.text
