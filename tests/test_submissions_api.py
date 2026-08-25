"""
the authenticated submission listing and detail routes

These are the first routes that can return what somebody typed into a form, so
what they hand back and what they refuse to hand back are both worth asserting
rather than assuming.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import (
    URLENCODED_HEADERS,
    ClientFactory,
    create_endpoint,
    seed_submission,
)
from hymical_forms.webhooks import DeliveryState

ENDPOINT = "/f/contact-form"
LISTING = "/submissions"

NOON = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def submit(client: TestClient, body: bytes = b"email=dev%40example.com") -> str:
    """
    send one submission through the public route
    :param client: the client to submit through
    :param body: the urlencoded body to send
    :returns: the identifier the API generated for it
    """
    response = client.post(ENDPOINT, content=body, headers=URLENCODED_HEADERS)
    assert response.status_code == 202, response.text
    return str(response.json()["submission_id"])


def listed(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    """
    read one page of the submission listing
    :param client: the client to read through
    :param params: query parameters to send
    :returns: the items on the page
    """
    response = client.get(LISTING, params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


# --- authentication ----------------------------------------------------------


@pytest.mark.parametrize("path", [LISTING, "/submissions/sub_whatever"])
def test_a_submission_route_requires_a_management_key(
    make_client: ClientFactory, path: str
) -> None:
    client = make_client(authenticate=False)

    response = client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_a_submission_route_refuses_a_bad_key(client: TestClient) -> None:
    response = client.get(LISTING, headers={"Authorization": "Bearer hym_live_nonsense"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# --- listing -----------------------------------------------------------------


def test_an_empty_service_lists_nothing(client: TestClient) -> None:
    body = client.get(LISTING).json()

    assert body["items"] == []
    assert body["next_cursor"] is None


def test_submissions_are_listed_newest_first(client: TestClient) -> None:
    first = submit(client)
    second = submit(client)
    third = submit(client)

    assert [item["id"] for item in listed(client)] == [third, second, first]


def test_a_summary_reports_the_submission_metadata(client: TestClient) -> None:
    submission_id = submit(client, b"email=dev%40example.com&topics=api&topics=billing")

    item = listed(client)[0]

    assert item["id"] == submission_id
    assert item["endpoint_id"] == "contact-form"
    assert item["field_count"] == 3
    assert item["idempotent"] is False
    assert datetime.fromisoformat(item["received_at"]).tzinfo is not None


def test_a_summary_never_carries_the_submitted_values(client: TestClient) -> None:
    """
    a listing is metadata, so paging a busy endpoint must not spread form content around
    :param client: test client whose app already holds the default endpoint
    """
    submit(client, b"secret=hunter2")

    item = listed(client)[0]

    assert "fields" not in item
    assert "hunter2" not in str(item)


def test_a_summary_reports_that_a_key_was_used_without_reporting_the_key(
    client: TestClient,
) -> None:
    key = "b8f1c2d4e5a67890b8f1c2d4e5a67890"
    client.post(
        ENDPOINT,
        content=b"email=dev%40example.com",
        headers=URLENCODED_HEADERS | {"Idempotency-Key": key},
    )

    item = listed(client)[0]

    assert item["idempotent"] is True
    assert key not in str(item)


def test_a_summary_carries_no_payload_fingerprint(client: TestClient) -> None:
    client.post(
        ENDPOINT,
        content=b"email=dev%40example.com",
        headers=URLENCODED_HEADERS | {"Idempotency-Key": "b8f1c2d4e5a67890b8f1c2d4e5a67890"},
    )

    assert "payload_fingerprint" not in str(listed(client)[0])


def test_a_submission_with_no_webhook_reports_no_delivery(client: TestClient) -> None:
    submit(client)

    assert listed(client)[0]["delivery"] is None


def test_a_submission_with_a_webhook_reports_its_delivery(
    make_client: ClientFactory, webhook: Any
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, webhook_url=webhook.url)
    submit(client)

    delivery = listed(client)[0]["delivery"]

    assert delivery["state"] == DeliveryState.PENDING
    assert delivery["attempt_count"] == 0
    assert delivery["id"].startswith("whd_")


# --- pagination --------------------------------------------------------------


def test_a_full_page_hands_back_a_cursor(client: TestClient) -> None:
    for _ in range(3):
        submit(client)

    body = client.get(LISTING, params={"limit": 2}).json()

    assert len(body["items"]) == 2
    assert body["next_cursor"] == body["items"][-1]["id"]


def test_a_cursor_continues_where_the_page_stopped(client: TestClient) -> None:
    identifiers = [submit(client) for _ in range(5)]

    first = client.get(LISTING, params={"limit": 2}).json()
    second = client.get(LISTING, params={"limit": 2, "cursor": first["next_cursor"]}).json()

    walked = [item["id"] for item in first["items"] + second["items"]]
    assert walked == list(reversed(identifiers))[:4]


def test_a_short_page_hands_back_no_cursor(client: TestClient) -> None:
    submit(client)

    assert client.get(LISTING, params={"limit": 50}).json()["next_cursor"] is None


def test_an_unknown_cursor_is_refused(client: TestClient) -> None:
    response = client.get(LISTING, params={"cursor": "sub_does_not_exist"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_a_page_size_above_the_ceiling_is_refused(client: TestClient) -> None:
    assert client.get(LISTING, params={"limit": 101}).status_code == 422


# --- filters -----------------------------------------------------------------


def test_the_endpoint_filter_narrows_the_page(make_client: ClientFactory) -> None:
    client = make_client()
    create_endpoint(client, "waitlist", name="Waitlist")
    mine = seed_submission(client, received_at=NOON, endpoint_id="waitlist")
    seed_submission(client, received_at=NOON, endpoint_id="contact-form")

    assert [item["id"] for item in listed(client, endpoint_id="waitlist")] == [mine]


def test_an_endpoint_with_nothing_lists_nothing(make_client: ClientFactory) -> None:
    client = make_client()
    create_endpoint(client, "waitlist", name="Waitlist")
    seed_submission(client, received_at=NOON)

    assert listed(client, endpoint_id="waitlist") == []


def test_received_after_is_strictly_exclusive(client: TestClient) -> None:
    """
    a bound that included its own instant would hand back the row it was taken from
    :param client: test client whose app already holds the default endpoint
    """
    on_the_bound = seed_submission(client, received_at=NOON)
    newer = seed_submission(client, received_at=NOON + timedelta(seconds=1))

    identifiers = [item["id"] for item in listed(client, received_after=NOON.isoformat())]

    assert identifiers == [newer]
    assert on_the_bound not in identifiers


def test_received_before_is_strictly_exclusive(client: TestClient) -> None:
    older = seed_submission(client, received_at=NOON - timedelta(seconds=1))
    on_the_bound = seed_submission(client, received_at=NOON)

    identifiers = [item["id"] for item in listed(client, received_before=NOON.isoformat())]

    assert identifiers == [older]
    assert on_the_bound not in identifiers


def test_both_bounds_together_select_the_window(client: TestClient) -> None:
    seed_submission(client, received_at=NOON - timedelta(hours=2))
    inside = seed_submission(client, received_at=NOON)
    seed_submission(client, received_at=NOON + timedelta(hours=2))

    identifiers = [
        item["id"]
        for item in listed(
            client,
            received_after=(NOON - timedelta(hours=1)).isoformat(),
            received_before=(NOON + timedelta(hours=1)).isoformat(),
        )
    ]

    assert identifiers == [inside]


def test_the_endpoint_and_time_filters_combine(make_client: ClientFactory) -> None:
    client = make_client()
    create_endpoint(client, "waitlist", name="Waitlist")
    wanted = seed_submission(client, received_at=NOON, endpoint_id="waitlist")
    seed_submission(client, received_at=NOON, endpoint_id="contact-form")
    seed_submission(client, received_at=NOON - timedelta(days=1), endpoint_id="waitlist")

    identifiers = [
        item["id"]
        for item in listed(
            client,
            endpoint_id="waitlist",
            received_after=(NOON - timedelta(hours=1)).isoformat(),
        )
    ]

    assert identifiers == [wanted]


@pytest.mark.parametrize("offset", [0, 1])
def test_an_impossible_time_range_is_refused(client: TestClient, offset: int) -> None:
    response = client.get(
        LISTING,
        params={
            "received_after": NOON.isoformat(),
            "received_before": (NOON - timedelta(seconds=offset)).isoformat(),
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_time_range"


def test_an_unparseable_timestamp_is_refused(client: TestClient) -> None:
    response = client.get(LISTING, params={"received_after": "yesterday"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


# --- detail ------------------------------------------------------------------


def test_a_known_submission_can_be_read_back(client: TestClient) -> None:
    submission_id = submit(client, b"email=dev%40example.com&message=hello")

    body = client.get(f"/submissions/{submission_id}").json()

    assert body["id"] == submission_id
    assert body["endpoint_id"] == "contact-form"
    assert body["field_count"] == 2
    assert body["fields"] == {"email": ["dev@example.com"], "message": ["hello"]}


def test_an_unknown_submission_is_a_stable_404(client: TestClient) -> None:
    response = client.get("/submissions/sub_does_not_exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "submission_not_found"


def test_repeated_values_survive_being_read_back(client: TestClient) -> None:
    """
    a checkbox group is what repeated field names are for, so the order has to hold
    :param client: test client whose app already holds the default endpoint
    """
    submission_id = submit(client, b"topics=billing&topics=api&topics=billing")

    fields = client.get(f"/submissions/{submission_id}").json()["fields"]

    assert fields == {"topics": ["billing", "api", "billing"]}


def test_a_single_value_stays_a_list(client: TestClient) -> None:
    submission_id = submit(client, b"email=dev%40example.com")

    fields = client.get(f"/submissions/{submission_id}").json()["fields"]

    assert fields == {"email": ["dev@example.com"]}


def test_detail_carries_no_internal_columns(client: TestClient) -> None:
    key = "b8f1c2d4e5a67890b8f1c2d4e5a67890"
    response = client.post(
        ENDPOINT,
        content=b"email=dev%40example.com",
        headers=URLENCODED_HEADERS | {"Idempotency-Key": key},
    )
    submission_id = response.json()["submission_id"]

    body = client.get(f"/submissions/{submission_id}").json()

    assert set(body) == {
        "id",
        "endpoint_id",
        "received_at",
        "field_count",
        "idempotent",
        "delivery",
        "fields",
    }
    assert key not in str(body)


def test_detail_carries_no_signing_secret(make_client: ClientFactory, webhook: Any) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    created = create_endpoint(client, webhook_url=webhook.url)
    submission_id = submit(client)

    body = client.get(f"/submissions/{submission_id}").json()

    assert created["webhook_secret"] not in str(body)
    assert "signing_secret" not in str(body)


def test_detail_reports_a_delivery_that_has_been_made(
    make_client: ClientFactory, webhook: Any
) -> None:
    client = make_client(seed_endpoint=False, allow_private_webhook_targets=True)
    create_endpoint(client, webhook_url=webhook.url)
    submission_id = seed_submission(
        client, received_at=NOON, delivery_state=DeliveryState.DELIVERED, attempts=2
    )

    delivery = client.get(f"/submissions/{submission_id}").json()["delivery"]

    assert delivery["state"] == DeliveryState.DELIVERED
    assert delivery["attempt_count"] == 2
