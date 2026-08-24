"""Endpoint identifier rules.

An endpoint ID is 3-64 characters of lowercase ASCII letters, digits, ``-`` and
``_``, and must start and end with a letter or digit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hymical_forms.ingestion import is_valid_endpoint_id

VALID_IDS = [
    "abc",
    "a1b",
    "contact-form",
    "signup_2026",
    "x" * 64,
]

INVALID_IDS = [
    "",
    "ab",  # shorter than the minimum
    "x" * 65,  # longer than the maximum
    "Contact",  # uppercase
    "-contact",  # leading separator
    "contact-",  # trailing separator
    "_contact",
    "contact_",
    "con tact",  # whitespace
    "con.tact",  # disallowed punctuation
    "contact\n",  # trailing newline must not slip past the pattern
    "café",  # non-ASCII
    "../etc",  # path traversal shapes
]


@pytest.mark.parametrize("endpoint_id", VALID_IDS)
def test_accepts_well_formed_identifiers(endpoint_id: str) -> None:
    assert is_valid_endpoint_id(endpoint_id)


@pytest.mark.parametrize("endpoint_id", INVALID_IDS)
def test_rejects_malformed_identifiers(endpoint_id: str) -> None:
    assert not is_valid_endpoint_id(endpoint_id)


@pytest.mark.parametrize("endpoint_id", VALID_IDS)
def test_valid_identifiers_are_addressable_over_http(client: TestClient, endpoint_id: str) -> None:
    response = client.post(f"/f/{endpoint_id}", data={"email": "dev@example.com"})

    assert response.status_code == 202
    assert response.json()["endpoint_id"] == endpoint_id


@pytest.mark.parametrize(
    "path_segment",
    ["Contact", "ab", "contact-", "con%20tact", "contact%0A", "con.tact", "caf%C3%A9"],
)
def test_invalid_identifiers_are_not_addressable(client: TestClient, path_segment: str) -> None:
    response = client.post(f"/f/{path_segment}", data={"email": "dev@example.com"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_endpoint_id"
