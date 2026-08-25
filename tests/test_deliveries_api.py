"""
reading the delivery queue through ``GET /deliveries`` and ``GET /deliveries/{id}``

Deliveries are driven into each state through the real ingestion path and the
real worker, so what is being listed is what the service would actually be
holding rather than rows a fixture invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import (
    ClientFactory,
    bearer,
    create_endpoint,
    management_key,
    open_session,
    work_once,
)
from hymical_forms import models, storage
from hymical_forms.webhooks import DeliveryState
from webhook_server import WebhookRecorder, unused_local_url

MANAGEMENT_ROUTES = [
    ("GET", "/deliveries"),
    ("GET", "/deliveries/whd_00000000000000000000000000000000"),
]


def queued_client(
    make_client: ClientFactory, url: str, **overrides: Any
) -> tuple[TestClient, dict[str, Any]]:
    """
    build a client whose default endpoint delivers to a given destination
    :param make_client: factory for clients bound to a configured app
    :param url: the webhook destination to configure
    :param overrides: extra setting overrides for the application
    :returns: the client and the created endpoint as the API returned it
    """
    overrides.setdefault("allow_private_webhook_targets", True)
    client = make_client(seed_endpoint=False, **overrides)
    endpoint = create_endpoint(client, webhook_url=url)
    return client, endpoint


def submit(client: TestClient, endpoint_id: str = "contact-form", **fields: str) -> str:
    """
    post a submission and read back the delivery it queued
    :param client: the client to submit through
    :param endpoint_id: the endpoint to address
    :param fields: the form fields to send
    :returns: the identifier of the delivery that was queued
    """
    data = fields or {"email": "dev@example.com"}
    response = client.post(f"/f/{endpoint_id}", data=data)
    assert response.status_code == 202, response.text
    submission_id = response.json()["submission_id"]
    with open_session(client) as session:
        delivery = session.scalars(
            select(models.WebhookDelivery).where(
                models.WebhookDelivery.submission_id == submission_id
            )
        ).one()
        return delivery.id


def states(client: TestClient) -> dict[str, str]:
    """
    read every delivery's state straight from the database
    :param client: the client whose application database should be inspected
    :returns: the state of each delivery, keyed by delivery id
    """
    with open_session(client) as session:
        return {row.id: row.state for row in session.scalars(select(models.WebhookDelivery))}


# --- authentication ----------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), MANAGEMENT_ROUTES)
def test_a_delivery_read_refuses_an_unauthenticated_request(
    make_client: ClientFactory, method: str, path: str
) -> None:
    client = make_client(authenticate=False)

    response = client.request(method, path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


@pytest.mark.parametrize(("method", "path"), MANAGEMENT_ROUTES)
def test_a_delivery_read_refuses_an_unusable_key(
    make_client: ClientFactory, method: str, path: str
) -> None:
    client = make_client(authenticate=False)

    response = client.request(method, path, headers=bearer("hym_live_nope"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# --- listing -----------------------------------------------------------------


def test_listing_with_nothing_queued_is_empty(client: TestClient) -> None:
    body = client.get("/deliveries").json()

    assert body == {"items": [], "next_cursor": None}


def test_a_listed_delivery_describes_the_work_that_is_owed(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = queued_client(make_client, webhook.url)
    delivery_id = submit(client)

    item = client.get("/deliveries").json()["items"][0]

    assert item["id"] == delivery_id
    assert item["endpoint_id"] == "contact-form"
    assert item["state"] == "pending"
    assert item["destination_url"] == endpoint["webhook_url"]
    assert item["attempt_count"] == 0
    assert item["cycle_attempt_count"] == 0
    assert item["completed_at"] is None
    assert item["submission_id"].startswith("sub_")


def test_listing_is_ordered_newest_first(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    created = [submit(client, email=f"dev{index}@example.com") for index in range(4)]

    listed = [item["id"] for item in client.get("/deliveries").json()["items"]]

    assert listed == list(reversed(created))


def test_paging_visits_every_delivery_exactly_once(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    created = [submit(client, email=f"dev{index}@example.com") for index in range(5)]

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/deliveries", params=params).json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert seen == list(reversed(created))


def test_the_same_page_request_answers_identically(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    for index in range(5):
        submit(client, email=f"dev{index}@example.com")

    first = client.get("/deliveries", params={"limit": 3}).json()
    again = client.get("/deliveries", params={"limit": 3}).json()

    assert first == again


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_an_unusable_page_size_is_refused(client: TestClient, limit: int) -> None:
    response = client.get("/deliveries", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_an_unknown_cursor_is_refused(client: TestClient) -> None:
    response = client.get("/deliveries", params={"cursor": "whd_nothing"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


# --- filtering ---------------------------------------------------------------


def test_deliveries_can_be_filtered_by_endpoint(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    create_endpoint(client, "waitlist", webhook_url=webhook.url)
    wanted = submit(client, "contact-form")
    submit(client, "waitlist")

    body = client.get("/deliveries", params={"endpoint_id": "contact-form"}).json()

    assert [item["id"] for item in body["items"]] == [wanted]


def test_filtering_by_an_endpoint_with_nothing_queued_is_empty(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    submit(client)

    body = client.get("/deliveries", params={"endpoint_id": "waitlist"}).json()

    assert body == {"items": [], "next_cursor": None}


def test_deliveries_can_be_filtered_by_state(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    every state the domain defines must be reachable through the filter
    :param make_client: factory for clients bound to a configured app
    :param webhook: a local server standing in for the receiver
    """
    client, _ = queued_client(make_client, webhook.url, worker_batch_size=1)
    delivered = submit(client, email="one@example.com")
    work_once(client)

    webhook.status = 400
    failed = submit(client, email="two@example.com")
    work_once(client)

    webhook.status = 200
    pending = submit(client, email="three@example.com")

    # Claimed and abandoned, exactly as a worker holding a lease would leave it.
    # The two terminal deliveries are not claimable, so the only one this can
    # take is the pending one.
    with open_session(client) as session:
        storage.claim_due_deliveries(
            session, now=datetime.now(UTC) + timedelta(hours=1), lease_seconds=3600, limit=1
        )
    processing = next(
        identifier
        for identifier, state in states(client).items()
        if state == DeliveryState.PROCESSING
    )

    assert processing == pending
    for state, expected in [
        ("delivered", delivered),
        ("failed", failed),
        ("processing", processing),
    ]:
        body = client.get("/deliveries", params={"state": state}).json()
        assert [item["id"] for item in body["items"]] == [expected], state


def test_the_two_filters_combine(make_client: ClientFactory, webhook: WebhookRecorder) -> None:
    webhook.status = 400
    client, _ = queued_client(make_client, webhook.url)
    create_endpoint(client, "waitlist", webhook_url=webhook.url)
    wanted = submit(client, "contact-form")
    submit(client, "waitlist")
    work_once(client)

    body = client.get(
        "/deliveries", params={"endpoint_id": "contact-form", "state": "failed"}
    ).json()

    assert [item["id"] for item in body["items"]] == [wanted]


def test_an_unknown_state_filter_is_refused(client: TestClient) -> None:
    response = client.get("/deliveries", params={"state": "exploded"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert [field["field"] for field in body["error"]["details"]["fields"]] == ["state"]


# --- detail ------------------------------------------------------------------


def test_detail_describes_one_delivery(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, endpoint = queued_client(make_client, webhook.url)
    delivery_id = submit(client)

    body = client.get(f"/deliveries/{delivery_id}").json()

    assert body["id"] == delivery_id
    assert body["endpoint_id"] == "contact-form"
    assert body["destination_url"] == endpoint["webhook_url"]
    assert body["attempts"] == []


def test_an_unknown_delivery_is_a_404(client: TestClient) -> None:
    response = client.get("/deliveries/whd_nothing")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "delivery_not_found"
    assert body["error"]["details"]["delivery_id"] == "whd_nothing"


def test_the_attempt_history_is_ordered_and_complete(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    webhook.status = 503
    client, _ = queued_client(
        make_client, webhook.url, webhook_max_attempts=3, webhook_retry_initial_seconds=10
    )
    delivery_id = submit(client)

    now = due_at(client, delivery_id)
    for _ in range(3):
        work_once(client, now=now)
        now = now + timedelta(minutes=5)

    body = client.get(f"/deliveries/{delivery_id}").json()

    assert [attempt["attempt_number"] for attempt in body["attempts"]] == [1, 2, 3]
    assert all(attempt["outcome"] == "http_error" for attempt in body["attempts"])
    assert all(attempt["response_status"] == 503 for attempt in body["attempts"])
    assert body["state"] == "failed"
    assert body["attempt_count"] == 3


def test_a_successful_attempt_reports_no_failure(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    delivery_id = submit(client)
    work_once(client)

    attempt = client.get(f"/deliveries/{delivery_id}").json()["attempts"][0]

    assert attempt["outcome"] == "succeeded"
    assert attempt["response_status"] == 200
    assert attempt["error"] is None


def test_a_network_failure_is_reported_without_a_status(make_client: ClientFactory) -> None:
    client, _ = queued_client(make_client, unused_local_url())
    delivery_id = submit(client)
    work_once(client)

    attempt = client.get(f"/deliveries/{delivery_id}").json()["attempts"][0]

    assert attempt["outcome"] == "network_error"
    assert attempt["response_status"] is None
    assert attempt["error"] is not None
    assert len(attempt["error"]) <= 500


def test_the_history_of_one_delivery_excludes_another(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    first = submit(client, email="one@example.com")
    submit(client, email="two@example.com")
    work_once(client)

    body = client.get(f"/deliveries/{first}").json()

    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["attempt_number"] == 1


# --- what must never be returned ---------------------------------------------


def test_a_delivery_read_never_carries_the_snapshotted_secret(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    """
    the secret is stored on the delivery, so its absence has to be asserted
    :param make_client: factory for clients bound to a configured app
    :param webhook: a local server standing in for the receiver
    """
    client, endpoint = queued_client(make_client, webhook.url)
    delivery_id = submit(client)
    work_once(client)
    secret = endpoint["webhook_secret"]

    listing = client.get("/deliveries")
    detail = client.get(f"/deliveries/{delivery_id}")

    assert secret not in listing.text
    assert secret not in detail.text
    assert "signing_secret" not in listing.text
    assert "signing_secret" not in detail.text


def test_a_delivery_read_never_carries_the_submitted_fields(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    delivery_id = submit(client, email="private@example.com", note="do not echo me")

    listing = client.get("/deliveries")
    detail = client.get(f"/deliveries/{delivery_id}")

    for response in (listing, detail):
        assert "private@example.com" not in response.text
        assert "do not echo me" not in response.text


def test_a_delivery_read_never_carries_management_credentials(
    make_client: ClientFactory, webhook: WebhookRecorder
) -> None:
    client, _ = queued_client(make_client, webhook.url)
    delivery_id = submit(client)
    key = management_key(client)

    detail = client.get(f"/deliveries/{delivery_id}")

    assert key not in detail.text
    assert "authorization" not in detail.text.lower()


def due_at(client: TestClient, delivery_id: str) -> datetime:
    """
    read the instant one delivery next becomes due
    :param client: the client whose application database should be inspected
    :param delivery_id: the delivery to read
    :returns: that delivery's next attempt time
    """
    with open_session(client) as session:
        delivery = session.get(models.WebhookDelivery, delivery_id)
        assert delivery is not None
        return delivery.next_attempt_at
