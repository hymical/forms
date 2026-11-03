"""
idempotency under real concurrency, across independent application instances

The unit suite races threads against SQLite. This races separate applications,
each with its own connection pool, against PostgreSQL, which is where the unique
constraint on ``(endpoint_id, idempotency_key)`` actually has to settle it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models
from hymical_forms.app import create_app
from integration.support import IsolatedSettings, seed_endpoint

ENDPOINT = "/f/contact-form"
KEY = "8f14e45f-ceea-467a-9575-7b0f2bb1b7a9"


def race(
    postgres_url: str, attempts: int, form_for: Callable[[int], dict[str, str]]
) -> list[Response]:
    """
    send one keyed submission from each of several applications at the same instant
    :param postgres_url: the database every application shares
    :param attempts: how many applications, and therefore how many submissions
    :param form_for: builds the form one attempt sends, from its index
    :returns: every response, in attempt order
    """
    barrier = threading.Barrier(attempts)

    def attempt(index: int) -> Response:
        app = create_app(IsolatedSettings(database_url=postgres_url, rate_limit_enabled=False))
        with TestClient(app) as client:
            barrier.wait()
            return client.post(ENDPOINT, data=form_for(index), headers={"Idempotency-Key": KEY})

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        return list(pool.map(attempt, range(attempts)))


def counts(sessions: sessionmaker[Session]) -> tuple[int, int]:
    """
    count what the race left behind
    :param sessions: factory handing out independent connections
    :returns: the number of submissions and of deliveries stored
    """
    with sessions() as session:
        submissions = session.scalar(select(func.count()).select_from(models.Submission))
        deliveries = session.scalar(select(func.count()).select_from(models.WebhookDelivery))
    return int(submissions or 0), int(deliveries or 0)


def test_concurrent_identical_retries_store_and_queue_exactly_once(
    postgres_url: str, sessions: sessionmaker[Session]
) -> None:
    with sessions() as session:
        seed_endpoint(session)

    responses = race(postgres_url, 8, lambda index: {"email": "dev@example.com"})

    assert [response.status_code for response in responses] == [202] * 8
    bodies = [response.json() for response in responses]
    assert len({body["submission_id"] for body in bodies}) == 1
    assert sum(not body["idempotent_replay"] for body in bodies) == 1
    assert all(body["delivery"]["queued"] for body in bodies)
    assert counts(sessions) == (1, 1)


def test_concurrent_conflicting_reuse_of_a_key_stores_exactly_one(
    postgres_url: str, sessions: sessionmaker[Session]
) -> None:
    with sessions() as session:
        seed_endpoint(session)

    responses = race(postgres_url, 8, lambda index: {"email": f"dev{index}@example.com"})

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [202] + [409] * 7
    assert all(
        response.json()["error"]["code"] == "idempotency_conflict"
        for response in responses
        if response.status_code == 409
    )
    assert counts(sessions) == (1, 1)
