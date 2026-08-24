"""Limits that protect the ingestion boundary."""

from __future__ import annotations

from collections.abc import Iterator

from conftest import URLENCODED_HEADERS, ClientFactory

ENDPOINT = "/f/contact-form"


def _chunks(*parts: bytes) -> Iterator[bytes]:
    """Yield a body in pieces so the client streams it without a Content-Length."""
    yield from parts


def test_rejects_a_body_larger_than_the_declared_limit(make_client: ClientFactory) -> None:
    client = make_client(max_body_bytes=64)

    response = client.post(ENDPOINT, content=b"note=" + b"x" * 200, headers=URLENCODED_HEADERS)

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "request_body_too_large"
    assert body["error"]["details"]["limit_bytes"] == 64


def test_rejects_an_oversized_streamed_body(make_client: ClientFactory) -> None:
    """A chunked request cannot escape the limit by omitting Content-Length."""
    client = make_client(max_body_bytes=64)

    response = client.post(
        ENDPOINT,
        content=_chunks(b"note=", b"x" * 200),
        headers=URLENCODED_HEADERS,
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"


def test_accepts_a_body_at_the_limit(make_client: ClientFactory) -> None:
    client = make_client(max_body_bytes=64)
    body = b"note=" + b"x" * 59

    response = client.post(ENDPOINT, content=body, headers=URLENCODED_HEADERS)

    assert len(body) == 64
    assert response.status_code == 202


def test_rejects_too_many_fields(make_client: ClientFactory) -> None:
    client = make_client(max_fields=3)

    response = client.post(ENDPOINT, data={f"field{i}": "v" for i in range(4)})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "too_many_fields"
    assert body["error"]["details"] == {"limit": 3, "received": 4}


def test_accepts_the_maximum_number_of_fields(make_client: ClientFactory) -> None:
    client = make_client(max_fields=3)

    response = client.post(ENDPOINT, data={f"field{i}": "v" for i in range(3)})

    assert response.status_code == 202
    assert response.json()["field_count"] == 3


def test_repeated_names_count_towards_the_field_limit(make_client: ClientFactory) -> None:
    client = make_client(max_fields=2)

    response = client.post(ENDPOINT, data={"topic": ["a", "b", "c"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "too_many_fields"


def test_rejects_an_overlong_field_name(make_client: ClientFactory) -> None:
    client = make_client(max_field_name_length=8)

    response = client.post(ENDPOINT, data={"a" * 9: "value"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "field_name_too_long"


def test_rejects_an_overlong_field_value(make_client: ClientFactory) -> None:
    client = make_client(max_field_value_length=8)

    response = client.post(ENDPOINT, data={"note": "x" * 9})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "field_value_too_long"
    assert body["error"]["details"]["field"] == "note"


def test_accepts_values_at_the_length_limit(make_client: ClientFactory) -> None:
    client = make_client(max_field_value_length=8)

    response = client.post(ENDPOINT, data={"note": "x" * 8})

    assert response.status_code == 202


def test_rejects_control_characters_in_a_field_name(make_client: ClientFactory) -> None:
    client = make_client()

    response = client.post(ENDPOINT, content=b"na%0Ame=value", headers=URLENCODED_HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_field_name"


def test_rejects_a_null_byte_in_a_field_value(make_client: ClientFactory) -> None:
    client = make_client()

    response = client.post(ENDPOINT, content=b"note=%00", headers=URLENCODED_HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_field_value"


def test_allows_newlines_inside_a_textarea_value(make_client: ClientFactory) -> None:
    """Multi-line textarea input is legitimate and must not trip the name rules."""
    client = make_client()

    response = client.post(ENDPOINT, data={"message": "line one\r\nline two"})

    assert response.status_code == 202
