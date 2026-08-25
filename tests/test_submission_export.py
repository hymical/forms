"""
exporting stored submissions as JSON and as CSV

The CSV is where the awkwardness lives: different submissions carry different
field names, a field name can be repeated, and both names and values are written
by whoever filled the form in. Most of what is asserted here is about that.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import URLENCODED_HEADERS, ClientFactory, create_endpoint, seed_submission
from hymical_forms import export

EXPORT = "/submissions/export"
ENDPOINT = "/f/contact-form"

NOON = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def exported_json(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    """
    read a JSON export and unwrap it
    :param client: the client to export through
    :param params: query parameters to send
    :returns: the exported submissions
    """
    response = client.get(EXPORT, params=params)
    assert response.status_code == 200, response.text
    return list(json.loads(response.text)["submissions"])


def exported_rows(client: TestClient, **params: Any) -> list[list[str]]:
    """
    read a CSV export and parse it back into rows
    :param client: the client to export through
    :param params: query parameters to send
    :returns: every row including the header, in order
    """
    response = client.get(EXPORT, params={"format": "csv", **params})
    assert response.status_code == 200, response.text
    return list(csv.reader(io.StringIO(response.text)))


# --- authentication ----------------------------------------------------------


@pytest.mark.parametrize("params", [{}, {"format": "csv"}])
def test_an_export_requires_a_management_key(
    make_client: ClientFactory, params: dict[str, str]
) -> None:
    client = make_client(authenticate=False)

    response = client.get(EXPORT, params=params)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# --- shared behaviour --------------------------------------------------------


def test_an_unknown_format_is_refused(client: TestClient) -> None:
    response = client.get(EXPORT, params={"format": "xlsx"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_export_format"


def test_an_impossible_time_range_is_refused(client: TestClient) -> None:
    response = client.get(
        EXPORT,
        params={
            "received_after": NOON.isoformat(),
            "received_before": (NOON - timedelta(days=1)).isoformat(),
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_time_range"


@pytest.mark.parametrize(
    ("params", "suffix"),
    [({}, "json"), ({"format": "csv"}, "csv")],
)
def test_an_export_is_offered_as_a_download(
    client: TestClient, params: dict[str, str], suffix: str
) -> None:
    seed_submission(client, received_at=NOON)

    disposition = client.get(EXPORT, params=params).headers["content-disposition"]

    assert disposition.startswith("attachment; ")
    assert disposition.endswith(f'.{suffix}"')
    assert "hymical-submissions-" in disposition


def test_an_export_filename_carries_nothing_a_caller_supplied(
    make_client: ClientFactory,
) -> None:
    """
    a filename built from user input is a header injection waiting to happen
    :param make_client: factory for clients bound to a configured app
    """
    client = make_client()
    create_endpoint(client, "quote-request", name="Quotes")
    seed_submission(client, received_at=NOON, endpoint_id="quote-request")

    disposition = client.get(EXPORT, params={"endpoint_id": "quote-request"}).headers[
        "content-disposition"
    ]

    assert "quote-request" not in disposition


def test_the_export_filters_match_the_listing(make_client: ClientFactory) -> None:
    client = make_client()
    create_endpoint(client, "waitlist", name="Waitlist")
    wanted = seed_submission(client, received_at=NOON, endpoint_id="waitlist")
    seed_submission(client, received_at=NOON, endpoint_id="contact-form")
    seed_submission(client, received_at=NOON - timedelta(days=2), endpoint_id="waitlist")

    exported = exported_json(
        client,
        endpoint_id="waitlist",
        received_after=(NOON - timedelta(days=1)).isoformat(),
    )

    assert [item["id"] for item in exported] == [wanted]


@pytest.mark.parametrize("params", [{}, {"format": "csv"}])
def test_an_export_larger_than_the_maximum_is_refused(
    make_client: ClientFactory, params: dict[str, str]
) -> None:
    """
    a truncated export is worse than an error, because nobody notices it
    :param make_client: factory for clients bound to a configured app
    :param params: the format to ask for
    """
    client = make_client(export_max_submissions=2)
    for index in range(3):
        seed_submission(client, received_at=NOON + timedelta(seconds=index))

    response = client.get(EXPORT, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "export_too_large"
    assert response.json()["error"]["details"]["limit"] == 2


def test_an_export_exactly_at_the_maximum_is_allowed(make_client: ClientFactory) -> None:
    client = make_client(export_max_submissions=2)
    for index in range(2):
        seed_submission(client, received_at=NOON + timedelta(seconds=index))

    assert len(exported_json(client)) == 2


def test_a_narrowed_filter_brings_an_export_back_under_the_maximum(
    make_client: ClientFactory,
) -> None:
    client = make_client(export_max_submissions=2)
    for index in range(3):
        seed_submission(client, received_at=NOON + timedelta(days=index))

    exported = exported_json(client, received_after=(NOON + timedelta(hours=12)).isoformat())

    assert len(exported) == 2


# --- json --------------------------------------------------------------------


def test_a_json_export_is_json(client: TestClient) -> None:
    seed_submission(client, received_at=NOON)

    response = client.get(EXPORT)

    assert response.headers["content-type"].startswith("application/json")


def test_a_json_export_carries_the_submitted_values(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"email": ["dev@example.com"]})

    assert exported_json(client)[0]["fields"] == {"email": ["dev@example.com"]}


def test_a_json_export_preserves_repeated_values(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"topics": ["billing", "api", "billing"]})

    assert exported_json(client)[0]["fields"] == {"topics": ["billing", "api", "billing"]}


def test_a_json_export_is_newest_first(client: TestClient) -> None:
    older = seed_submission(client, received_at=NOON - timedelta(hours=1))
    newer = seed_submission(client, received_at=NOON)

    assert [item["id"] for item in exported_json(client)] == [newer, older]


def test_a_json_export_carries_no_internal_columns(client: TestClient) -> None:
    client.post(
        ENDPOINT,
        content=b"email=dev%40example.com",
        headers=URLENCODED_HEADERS | {"Idempotency-Key": "b8f1c2d4e5a67890b8f1c2d4e5a67890"},
    )

    exported = exported_json(client)[0]

    assert set(exported) == {"id", "endpoint_id", "received_at", "fields"}


def test_an_empty_json_export_is_still_a_document(client: TestClient) -> None:
    response = client.get(EXPORT)

    assert response.status_code == 200
    assert json.loads(response.text) == {"submissions": []}


# --- csv ---------------------------------------------------------------------


def test_a_csv_export_is_csv(client: TestClient) -> None:
    seed_submission(client, received_at=NOON)

    response = client.get(EXPORT, params={"format": "csv"})

    assert response.headers["content-type"].startswith("text/csv")


def test_a_csv_export_starts_with_the_metadata_columns(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"email": ["dev@example.com"]})

    header = exported_rows(client)[0]

    assert header[:3] == ["submission_id", "endpoint_id", "received_at"]
    assert header[3:] == ["email"]


def test_a_csv_header_is_the_union_of_every_field_name(client: TestClient) -> None:
    seed_submission(client, received_at=NOON - timedelta(hours=1), fields={"email": ["a@b.c"]})
    seed_submission(client, received_at=NOON, fields={"email": ["d@e.f"], "phone": ["123"]})

    header = exported_rows(client)[0]

    assert set(header[3:]) == {"email", "phone"}


def test_a_missing_field_becomes_an_empty_cell(client: TestClient) -> None:
    """
    an absent field and a field submitted empty are different things
    :param client: test client whose app already holds the default endpoint
    """
    seed_submission(client, received_at=NOON - timedelta(hours=1), fields={"phone": [""]})
    seed_submission(client, received_at=NOON, fields={"email": ["d@e.f"]})

    header, newer, older = exported_rows(client)
    phone = header.index("phone")

    assert newer[phone] == ""
    assert older[phone] == '[""]'


def test_repeated_values_are_one_cell_holding_an_array(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"topics": ["api", "billing"]})

    header, row = exported_rows(client)

    assert row[header.index("topics")] == '["api","billing"]'


def test_a_single_value_is_still_an_array(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"email": ["dev@example.com"]})

    header, row = exported_rows(client)

    assert row[header.index("email")] == '["dev@example.com"]'


@pytest.mark.parametrize(
    "value",
    [
        "one, two",
        'she said "hello"',
        "first line\nsecond line",
        "tab\tseparated",
        "back\\slash",
    ],
)
def test_awkward_characters_survive_a_round_trip(client: TestClient, value: str) -> None:
    """
    a comma, a quote or a newline in an answer must come back out as it went in
    :param client: test client whose app already holds the default endpoint
    :param value: the submitted value to round trip
    """
    seed_submission(client, received_at=NOON, fields={"message": [value]})

    header, row = exported_rows(client)

    assert json.loads(row[header.index("message")]) == [value]


def test_unicode_survives_a_round_trip(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"naam": ["Zoë", "日本語", "🙂"]})

    header, row = exported_rows(client)

    assert json.loads(row[header.index("naam")]) == ["Zoë", "日本語", "🙂"]


def test_a_csv_export_is_utf8(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"naam": ["Zoë"]})

    response = client.get(EXPORT, params={"format": "csv"})

    assert "charset=utf-8" in response.headers["content-type"]
    assert "Zoë".encode() in response.content


def test_an_empty_csv_export_is_an_empty_document(client: TestClient) -> None:
    rows = exported_rows(client)

    assert rows == [["submission_id", "endpoint_id", "received_at"]]


# --- formula injection -------------------------------------------------------


def test_a_value_that_looks_like_a_formula_is_never_at_the_start_of_a_cell(
    client: TestClient,
) -> None:
    """
    the array encoding is what makes a value cell start with a bracket rather than an equals
    :param client: test client whose app already holds the default endpoint
    """
    seed_submission(client, received_at=NOON, fields={"message": ["=1+1"]})

    header, row = exported_rows(client)
    cell = row[header.index("message")]

    assert cell.startswith("[")
    # And the value itself is still there to be read, unaltered.
    assert json.loads(cell) == ["=1+1"]


@pytest.mark.parametrize("name", ["=cmd()", "+1", "-1", "@SUM(A1)"])
def test_a_field_name_that_looks_like_a_formula_is_marked_as_text(
    client: TestClient, name: str
) -> None:
    """
    a field name becomes a header cell, and a form is free to call a field anything
    :param client: test client whose app already holds the default endpoint
    :param name: the submitted field name to export
    """
    seed_submission(client, received_at=NOON, fields={name: ["value"]})

    header = exported_rows(client)[0]

    assert header[3] == f"'{name}"


def test_an_ordinary_field_name_is_left_alone(client: TestClient) -> None:
    seed_submission(client, received_at=NOON, fields={"email": ["dev@example.com"]})

    assert exported_rows(client)[0][3] == "email"


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@x", "\tx", "\rx"])
def test_the_escaping_rule_marks_every_formula_leader(value: str) -> None:
    marked = export._safe(value)

    assert marked.startswith("'")
    assert marked.removeprefix("'") == value


@pytest.mark.parametrize("value", ["email", "[1]", "1+1", "", "x=1"])
def test_the_escaping_rule_leaves_anything_else_alone(value: str) -> None:
    assert export._safe(value) == value


# --- logging -----------------------------------------------------------------


@pytest.mark.parametrize("params", [{}, {"format": "csv"}])
def test_an_export_logs_who_and_how_much_but_never_what(
    client: TestClient, caplog: pytest.LogCaptureFixture, params: dict[str, str]
) -> None:
    """
    an export moves form content on purpose, so duplicating it into the log undoes that
    :param client: test client whose app already holds the default endpoint
    :param caplog: pytest fixture capturing what was logged
    :param params: the format to ask for
    """
    seed_submission(
        client, received_at=NOON, fields={"confession": ["something private"], "topics": ["api"]}
    )

    with caplog.at_level("DEBUG"):
        assert client.get(EXPORT, params=params).status_code == 200

    logged = caplog.text
    assert "confession" not in logged
    assert "something private" not in logged
    assert "1 submission(s) exported" in logged


def test_reading_one_submission_back_logs_nothing_about_it(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    submission_id = seed_submission(
        client, received_at=NOON, fields={"confession": ["something private"]}
    )

    with caplog.at_level("DEBUG"):
        assert client.get(f"/submissions/{submission_id}").status_code == 200

    assert "confession" not in caplog.text
    assert "something private" not in caplog.text
