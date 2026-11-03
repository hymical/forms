"""
malformed management input against the database it would otherwise reach

PostgreSQL refuses a NUL character in any text value, so a NUL that got as far
as a query surfaced as a storage error and was answered 503. These requests are
asserted here, against the real database, because SQLite accepts a NUL and so
the fast suite alone could not show the difference.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from integration.support import seed_endpoint

NUL_REQUESTS = [
    ("GET", "/endpoints/a%00b", None, 404),
    ("PATCH", "/endpoints/a%00b", {"is_active": False}, 404),
    ("GET", "/deliveries/a%00b", None, 404),
    ("POST", "/deliveries/a%00b/replay", None, 404),
    ("GET", "/submissions/a%00b", None, 404),
    ("GET", "/endpoints?cursor=a%00b", None, 422),
    ("GET", "/deliveries?endpoint_id=a%00b", None, 422),
    ("GET", "/submissions?endpoint_id=a%00b", None, 422),
    ("GET", "/submissions/export?endpoint_id=a%00b", None, 422),
    ("POST", "/endpoints", {"id": "abc", "name": "a\x00b"}, 422),
    ("PATCH", "/endpoints/contact-form", {"name": "a\x00b"}, 422),
    (
        "POST",
        "/endpoints",
        {"id": "abd", "name": "a", "webhook_url": "https://x.example/\x00"},
        422,
    ),
]


@pytest.mark.parametrize(("method", "path", "body", "status"), NUL_REQUESTS)
def test_a_nul_character_never_reaches_the_database(
    pg_client: TestClient,
    sessions: sessionmaker[Session],
    method: str,
    path: str,
    body: dict[str, object] | None,
    status: int,
) -> None:
    with sessions() as session:
        seed_endpoint(session)

    response = pg_client.request(method, path, json=body)

    assert response.status_code == status
    assert response.json()["error"]["code"] != "storage_unavailable"
