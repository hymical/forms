"""
public ingestion rate limiting against real PostgreSQL

This is the behaviour SQLite cannot show. SQLite serialises writers, so the fast
suite can only demonstrate that the arithmetic is right. What matters in
production is that several API processes holding several connections cannot
between them let more traffic through than one of them would, and that can only
be shown here.

Every application in this module is built on its own, with its own engine and its
own connection pool, so two of them are as independent as two deployed replicas.
Nothing is mocked and no session is shared: the only thing these applications
have in common is the database, which is exactly the claim being tested.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models
from hymical_forms.app import create_app
from hymical_forms.ratelimit import Limiter
from integration.support import IsolatedSettings, seed_endpoint

ENDPOINT = "/f/contact-form"
FORM = {"email": "dev@example.com"}

IP_ALLOWANCE = 3
ENDPOINT_ALLOWANCE = 4

# Long enough that no test can cross a window boundary while it runs, so an exact
# total is an exact total rather than a race with the clock.
WINDOW_SECONDS = 3600


def no_headers(index: int) -> dict[str, str]:
    """
    build the headers an attempt that claims no forwarded address sends
    :param index: which attempt this is, which makes no difference here
    :returns: an empty header mapping
    """
    return {}


def build_app(postgres_url: str, **overrides: Any) -> FastAPI:
    """
    build an application with a connection pool of its own
    :param postgres_url: the database every application in a test shares
    :param overrides: setting values to replace the built-in defaults
    :returns: an application as independent of the others as a separate replica
    """
    return create_app(
        IsolatedSettings(
            database_url=postgres_url,
            rate_limit_ip_window_seconds=WINDOW_SECONDS,
            rate_limit_endpoint_window_seconds=WINDOW_SECONDS,
            **overrides,
        )
    )


def fire_together(
    postgres_url: str,
    attempts: int,
    *,
    headers_for: Callable[[int], dict[str, str]] = no_headers,
    **overrides: Any,
) -> list[int]:
    """
    submit once from each of several independent applications at the same instant
    :param postgres_url: the database every application shares
    :param attempts: how many applications, and therefore how many attempts
    :param headers_for: builds the headers one attempt sends, from its index
    :param overrides: setting values every application is built with
    :returns: the status code each attempt received
    """
    # The application and its client are built before the barrier, so what the
    # attempts genuinely share is the moment they post rather than the moment they
    # started connecting.
    barrier = threading.Barrier(attempts)

    def attempt(index: int) -> int:
        with TestClient(build_app(postgres_url, **overrides)) as client:
            barrier.wait()
            return client.post(ENDPOINT, data=FORM, headers=headers_for(index)).status_code

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        futures = [pool.submit(attempt, index) for index in range(attempts)]
        return [future.result() for future in futures]


def counter_total(sessions: sessionmaker[Session], limiter: Limiter) -> int:
    """
    total what one limiter recorded, read on a connection of its own
    :param sessions: factory handing out independent connections
    :param limiter: which limiter's counters to total
    :returns: the sum of the attempts it recorded
    """
    with sessions() as session:
        rows = list(session.scalars(select(models.RateLimitCounter)))
    return sum(row.attempts for row in rows if row.limiter == limiter)


def test_concurrent_attempts_from_one_source_cannot_exceed_its_allowance(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """
    ten processes must let exactly the configured number through, not ten
    :param postgres_url: the database every application shares
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: factory handing out independent connections
    """
    attempts = 10
    with sessions() as setup:
        seed_endpoint(setup)

    statuses = fire_together(
        postgres_url,
        attempts,
        rate_limit_ip_requests=IP_ALLOWANCE,
        rate_limit_endpoint_requests=1000,
    )

    assert statuses.count(202) == IP_ALLOWANCE
    assert statuses.count(429) == attempts - IP_ALLOWANCE
    assert set(statuses) == {202, 429}

    # And the database agrees, end to end: every attempt was counted, and exactly
    # the allowed ones left a submission behind.
    assert counter_total(sessions, Limiter.IP) == attempts
    with sessions() as session:
        stored = list(session.scalars(select(models.Submission)))
    assert len(stored) == IP_ALLOWANCE


def test_concurrent_attempts_from_many_sources_cannot_exceed_the_endpoint_allowance(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """
    an attack spread over addresses is exactly what the endpoint limit answers
    :param postgres_url: the database every application shares
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: factory handing out independent connections
    """
    attempts = 10
    with sessions() as setup:
        seed_endpoint(setup)

    statuses = fire_together(
        postgres_url,
        attempts,
        # Every attempt arrives from a different address, so no address budget can
        # be what refused any of them.
        headers_for=lambda index: {"X-Forwarded-For": f"203.0.113.{index + 1}"},
        trusted_proxy_hops=1,
        rate_limit_ip_requests=1000,
        rate_limit_endpoint_requests=ENDPOINT_ALLOWANCE,
    )

    assert statuses.count(202) == ENDPOINT_ALLOWANCE
    assert statuses.count(429) == attempts - ENDPOINT_ALLOWANCE
    assert counter_total(sessions, Limiter.ENDPOINT) == attempts


def test_no_increment_is_lost_when_many_processes_count_at_once(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """
    a read-compare-write would lose updates here and the total would come up short
    :param postgres_url: the database every application shares
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: factory handing out independent connections
    """
    attempts = 12
    with sessions() as setup:
        seed_endpoint(setup)

    statuses = fire_together(
        postgres_url,
        attempts,
        rate_limit_ip_requests=1000,
        rate_limit_endpoint_requests=1000,
    )

    assert statuses == [202] * attempts
    assert counter_total(sessions, Limiter.IP) == attempts
    assert counter_total(sessions, Limiter.ENDPOINT) == attempts


def test_one_source_counter_holds_every_concurrent_attempt(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """
    the whole point of one shared row is that it is one row, not one per process
    :param postgres_url: the database every application shares
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)

    fire_together(postgres_url, 8, rate_limit_ip_requests=1000, rate_limit_endpoint_requests=1000)

    with sessions() as session:
        rows = list(session.scalars(select(models.RateLimitCounter)))
    by_limiter = {row.limiter: row.attempts for row in rows}
    assert len(rows) == 2, "the processes did not share one counter per limiter"
    assert by_limiter == {str(Limiter.IP): 8, str(Limiter.ENDPOINT): 8}


def test_a_budget_one_process_spent_is_already_spent_for_the_next(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """
    an in-memory limiter would pass this second application every time
    :param postgres_url: the database every application shares
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
    overrides: dict[str, Any] = {
        "rate_limit_ip_requests": IP_ALLOWANCE,
        "rate_limit_endpoint_requests": 1000,
    }

    with TestClient(build_app(postgres_url, **overrides)) as first:
        spent = [first.post(ENDPOINT, data=FORM).status_code for _ in range(IP_ALLOWANCE)]

    # A whole other application, built afterwards, with an engine and a pool it
    # does not share with the first one. It has never seen this address before.
    with TestClient(build_app(postgres_url, **overrides)) as second:
        response = second.post(ENDPOINT, data=FORM)

    assert spent == [202] * IP_ALLOWANCE
    assert response.status_code == 429
    assert response.json()["error"]["details"]["scope"] == "ip"
    assert response.headers["Retry-After"].isdigit()


def test_two_endpoints_do_not_share_a_budget_under_load(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> None:
    """
    flooding one endpoint must not refuse traffic addressed to another
    :param postgres_url: the database every application shares
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: factory handing out independent connections
    """
    with sessions() as setup:
        seed_endpoint(setup)
        seed_endpoint(setup, endpoint_id="second-form")

    fire_together(
        postgres_url,
        6,
        headers_for=lambda index: {"X-Forwarded-For": f"203.0.113.{index + 1}"},
        trusted_proxy_hops=1,
        rate_limit_ip_requests=1000,
        rate_limit_endpoint_requests=ENDPOINT_ALLOWANCE,
    )

    with TestClient(
        build_app(
            postgres_url,
            rate_limit_ip_requests=1000,
            rate_limit_endpoint_requests=ENDPOINT_ALLOWANCE,
        )
    ) as client:
        assert client.post(ENDPOINT, data=FORM).status_code == 429
        assert client.post("/f/second-form", data=FORM).status_code == 202
