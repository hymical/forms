"""
traffic rate limiting on public form ingestion

These tests are about the ordering as much as the arithmetic. What counts as an
attempt, which limiter a refused attempt has already spent, and what is charged
before the endpoint is even known are all decisions rather than accidents, so
each one is asserted rather than assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from conftest import (
    URLENCODED_HEADERS,
    ClientFactory,
    build_settings,
    create_endpoint,
    open_session,
)
from hymical_forms import models, storage
from hymical_forms.ratelimit import (
    UNKNOWN_CLIENT,
    Limiter,
    RateLimit,
    client_address,
    ip_subject,
    seconds_until_window_ends,
    window_start,
)

ENDPOINT = "/f/contact-form"
FORM = {"email": "dev@example.com"}

# Mid-window for a sixty second window, so the wait a refusal reports is a round
# number and the boundary the next window starts on is unambiguous.
NOON = datetime(2026, 8, 24, 12, 0, 30, tzinfo=UTC)

MULTIPART_HEADERS = {"content-type": "multipart/form-data; boundary=hymical"}


class Clock:
    """
    a stand-in for the wall clock that a test moves deliberately
    """

    def __init__(self, start: datetime) -> None:
        """
        start the clock at an instant
        :param start: the instant the clock reads to begin with
        """
        self.now = start

    def advance(self, seconds: float) -> None:
        """
        move the clock forward
        :param seconds: how far forward to move it
        """
        self.now += timedelta(seconds=seconds)

    def __call__(self) -> datetime:
        """
        read the clock
        :returns: the instant the clock currently reads
        """
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """
    freeze the clock the ingestion route stamps rate limit windows from
    :param monkeypatch: pytest fixture used to replace the route's clock
    :returns: the clock, which the test moves itself
    """
    # A window is an interval of wall-clock time, so expiry can only be tested by
    # controlling the clock. Sleeping through a real window would make the suite
    # slow and would still be timing dependent.
    frozen = Clock(NOON)
    monkeypatch.setattr("hymical_forms.api.submissions.utcnow", frozen)
    return frozen


def counters(client: TestClient) -> list[models.RateLimitCounter]:
    """
    read every rate limit counter behind a client
    :param client: the client whose application database should be inspected
    :returns: the counter rows
    """
    with open_session(client) as session:
        return list(session.scalars(select(models.RateLimitCounter)))


def spent(client: TestClient, limiter: Limiter) -> int:
    """
    total the attempts recorded against one limiter
    :param client: the client whose application database should be inspected
    :param limiter: which limiter's counters to total
    :returns: the sum of the attempts it has recorded
    """
    return sum(row.attempts for row in counters(client) if row.limiter == limiter)


def submit(client: TestClient, **kwargs: Any) -> int:
    """
    post the default form and report only the status
    :param client: the client to submit through
    :param kwargs: extra arguments passed straight to the request
    :returns: the response status code
    """
    return client.post(ENDPOINT, data=FORM, **kwargs).status_code


def forwarded(address: str) -> dict[str, str]:
    """
    build the header a reverse proxy would have appended
    :param address: the address the proxy is claiming to have seen
    :returns: an ``X-Forwarded-For`` header carrying it
    """
    return {"X-Forwarded-For": address}


# --- configuration -----------------------------------------------------------


def test_rate_limiting_is_enabled_by_default() -> None:
    settings = build_settings()

    assert settings.rate_limit_enabled is True
    assert settings.ip_rate_limit() == RateLimit(requests=60, window_seconds=60)
    assert settings.endpoint_rate_limit() == RateLimit(requests=600, window_seconds=60)


def test_the_default_trust_model_reads_no_forwarding_header() -> None:
    """
    a spoofable header must not become authoritative without an operator saying so
    """
    settings = build_settings()

    assert settings.trusted_proxy_hops == 0
    assert settings.rate_limit_ip_secret is None


@pytest.mark.parametrize(
    "override",
    [
        {"rate_limit_ip_requests": 0},
        {"rate_limit_ip_requests": -1},
        {"rate_limit_endpoint_requests": 0},
        {"rate_limit_endpoint_requests": -5},
    ],
)
def test_a_limit_that_allows_nothing_is_refused(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**override)


@pytest.mark.parametrize(
    "override",
    [
        {"rate_limit_ip_window_seconds": 0},
        {"rate_limit_ip_window_seconds": -60},
        {"rate_limit_endpoint_window_seconds": 0},
        {"rate_limit_endpoint_window_seconds": -1},
    ],
)
def test_a_window_with_no_length_is_refused(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**override)


def test_a_negative_proxy_hop_count_is_refused() -> None:
    with pytest.raises(ValidationError):
        build_settings(trusted_proxy_hops=-1)


def test_a_pointlessly_short_address_secret_is_refused() -> None:
    """
    a secret that is guessable does not make the digest it keys one-way
    """
    with pytest.raises(ValidationError):
        build_settings(rate_limit_ip_secret="short")


def test_disabling_rate_limiting_lets_every_attempt_through(make_client: ClientFactory) -> None:
    client = make_client(rate_limit_enabled=False, rate_limit_ip_requests=1)

    statuses = [submit(client) for _ in range(4)]

    assert statuses == [202, 202, 202, 202]
    assert counters(client) == [], "a disabled limiter still wrote counters"


# --- per source address ------------------------------------------------------


def test_attempts_below_the_limit_are_accepted(make_client: ClientFactory, clock: Clock) -> None:
    client = make_client(rate_limit_ip_requests=3)

    assert [submit(client) for _ in range(2)] == [202, 202]


def test_the_attempt_that_reaches_the_limit_is_still_accepted(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    the boundary is inclusive: a limit of three means three attempts, not two
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=3)

    assert [submit(client) for _ in range(3)] == [202, 202, 202]
    assert spent(client, Limiter.IP) == 3


def test_the_attempt_after_the_limit_is_refused(make_client: ClientFactory, clock: Clock) -> None:
    client = make_client(rate_limit_ip_requests=3, rate_limit_ip_window_seconds=60)
    for _ in range(3):
        submit(client)

    response = client.post(ENDPOINT, data=FORM)

    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert body["error"]["details"] == {
        "scope": "ip",
        "limit": 3,
        "window_seconds": 60,
        "retry_after_seconds": 30,
    }


def test_a_refusal_says_how_long_to_wait(make_client: ClientFactory, clock: Clock) -> None:
    client = make_client(rate_limit_ip_requests=1, rate_limit_ip_window_seconds=60)
    submit(client)

    response = client.post(ENDPOINT, data=FORM)

    # The clock sits thirty seconds into a sixty second window, so that is exactly
    # how long is left of the window that refused it.
    assert response.headers["Retry-After"] == "30"


def test_the_refusal_names_no_internal_state(make_client: ClientFactory, clock: Clock) -> None:
    """
    a 429 must not become a way to read the counter table
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=1)
    submit(client)

    body = client.post(ENDPOINT, data=FORM).json()

    rendered = repr(body)
    assert "rate_limit_counters" not in rendered
    assert "subject" not in rendered
    assert "window_start" not in rendered
    assert "testclient" not in rendered


def test_the_budget_returns_with_the_next_window(make_client: ClientFactory, clock: Clock) -> None:
    """
    a fixed window has to actually end, and only on its boundary
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=1, rate_limit_ip_window_seconds=60)
    submit(client)

    # One second short of the boundary the window is still the same window.
    clock.advance(29)
    assert submit(client) == 429

    clock.advance(1)
    assert submit(client) == 202


def test_one_source_does_not_spend_another_sources_budget(
    make_client: ClientFactory, clock: Clock
) -> None:
    client = make_client(trusted_proxy_hops=1, rate_limit_ip_requests=2)
    for _ in range(2):
        submit(client, headers=forwarded("203.0.113.1"))

    assert submit(client, headers=forwarded("203.0.113.1")) == 429
    assert submit(client, headers=forwarded("203.0.113.2")) == 202


# --- per endpoint ------------------------------------------------------------


def test_an_endpoint_reaches_its_own_limit(make_client: ClientFactory, clock: Clock) -> None:
    client = make_client(rate_limit_endpoint_requests=2, rate_limit_ip_requests=100)

    statuses = [submit(client) for _ in range(3)]

    assert statuses == [202, 202, 429]
    assert client.post(ENDPOINT, data=FORM).json()["error"]["details"]["scope"] == "endpoint"


def test_another_endpoint_stays_usable(make_client: ClientFactory, clock: Clock) -> None:
    """
    one endpoint being flooded must not take the rest of the service down with it
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_endpoint_requests=2, rate_limit_ip_requests=100)
    create_endpoint(client, "second-form", name="Second form")
    for _ in range(3):
        submit(client)

    assert submit(client) == 429
    assert client.post("/f/second-form", data=FORM).status_code == 202


def test_distributed_sources_still_spend_the_endpoint_budget(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    the endpoint limit is what answers an attack spread across many addresses
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(
        trusted_proxy_hops=1,
        rate_limit_endpoint_requests=3,
        rate_limit_ip_requests=100,
    )

    statuses = [submit(client, headers=forwarded(f"203.0.113.{n}")) for n in range(1, 6)]

    # Every attempt came from a different address and none of them exhausted an
    # address budget, so only the shared endpoint budget can have refused them.
    assert statuses == [202, 202, 202, 429, 429]
    assert spent(client, Limiter.ENDPOINT) == 5


def test_the_endpoint_budget_returns_with_the_next_window(
    make_client: ClientFactory, clock: Clock
) -> None:
    client = make_client(
        rate_limit_endpoint_requests=1,
        rate_limit_endpoint_window_seconds=60,
        rate_limit_ip_requests=100,
    )
    submit(client)
    assert submit(client) == 429

    clock.advance(30)

    assert submit(client) == 202


# --- both limits together ----------------------------------------------------


def test_the_source_limit_can_be_the_one_that_refuses(
    make_client: ClientFactory, clock: Clock
) -> None:
    client = make_client(rate_limit_ip_requests=1, rate_limit_endpoint_requests=100)
    submit(client)

    body = client.post(ENDPOINT, data=FORM).json()

    assert body["error"]["details"]["scope"] == "ip"


def test_the_endpoint_limit_can_be_the_one_that_refuses(
    make_client: ClientFactory, clock: Clock
) -> None:
    client = make_client(rate_limit_ip_requests=100, rate_limit_endpoint_requests=1)
    submit(client)

    body = client.post(ENDPOINT, data=FORM).json()

    assert body["error"]["details"]["scope"] == "endpoint"


def test_an_attempt_the_endpoint_limit_refuses_still_spends_the_source_budget(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    otherwise one saturated endpoint becomes a free target for one address
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=4, rate_limit_endpoint_requests=1)

    statuses = [submit(client) for _ in range(4)]

    # The first attempt spent the endpoint's whole budget and the next three were
    # refused by it, but all four spent a unit of the address budget, so the fifth
    # is refused by the address limit rather than the endpoint one.
    assert statuses == [202, 429, 429, 429]
    assert spent(client, Limiter.IP) == 4
    assert client.post(ENDPOINT, data=FORM).json()["error"]["details"]["scope"] == "ip"


def test_an_attempt_the_source_limit_refuses_does_not_spend_the_endpoint_budget(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    a blocked address must not be able to burn the budget of an endpoint it cannot reach
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=1, rate_limit_endpoint_requests=100)
    submit(client)
    for _ in range(5):
        assert submit(client) == 429

    assert spent(client, Limiter.IP) == 6
    assert spent(client, Limiter.ENDPOINT) == 1


def test_the_wait_belongs_to_the_limiter_that_refused(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    two limiters can run different windows, so the wait must come from the right one
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(
        rate_limit_ip_requests=100,
        rate_limit_ip_window_seconds=60,
        rate_limit_endpoint_requests=1,
        rate_limit_endpoint_window_seconds=10,
    )
    submit(client)

    response = client.post(ENDPOINT, data=FORM)

    # The address window has thirty seconds left and the endpoint window has ten.
    # The endpoint limiter is the one that refused, so ten is the honest answer.
    assert response.headers["Retry-After"] == "10"
    assert response.json()["error"]["details"]["scope"] == "endpoint"


# --- what an attempt is ------------------------------------------------------


def test_an_oversized_body_is_refused_before_the_limiter_sees_it(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    the body cap runs in middleware, so the limiter never has to hold the body
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(max_body_bytes=64, rate_limit_ip_requests=1)

    response = client.post(ENDPOINT, content=b"note=" + b"x" * 200, headers=URLENCODED_HEADERS)

    assert response.status_code == 413
    assert counters(client) == [], "a body the middleware refused reached the limiter"
    assert submit(client) == 202, "an oversized body spent a budget it never reached"


def test_an_unknown_endpoint_spends_only_the_source_budget(
    client: TestClient, clock: Clock
) -> None:
    """
    a guessed identifier must not let an attacker choose how much this table grows
    :param client: test client whose app holds the default endpoint
    :param clock: the frozen clock the route stamps windows from
    """
    assert client.post("/f/no-such-form", data=FORM).status_code == 404

    assert spent(client, Limiter.IP) == 1
    assert spent(client, Limiter.ENDPOINT) == 0


def test_a_malformed_endpoint_id_still_spends_the_source_budget(
    client: TestClient, clock: Clock
) -> None:
    assert client.post("/f/NOPE", data=FORM).status_code == 404

    assert spent(client, Limiter.IP) == 1
    assert spent(client, Limiter.ENDPOINT) == 0


def test_a_malformed_body_still_spends_both_budgets(client: TestClient, clock: Clock) -> None:
    """
    invalid traffic still costs this service work, so it still costs the sender budget
    :param client: test client whose app holds the default endpoint
    :param clock: the frozen clock the route stamps windows from
    """
    response = client.post(ENDPOINT, content=b"not multipart", headers=MULTIPART_HEADERS)

    assert response.status_code == 400
    assert spent(client, Limiter.IP) == 1
    assert spent(client, Limiter.ENDPOINT) == 1


def test_an_unsupported_content_type_still_spends_both_budgets(
    client: TestClient, clock: Clock
) -> None:
    response = client.post(ENDPOINT, content=b"{}", headers={"content-type": "application/json"})

    assert response.status_code == 415
    assert spent(client, Limiter.IP) == 1
    assert spent(client, Limiter.ENDPOINT) == 1


def test_an_empty_submission_still_spends_both_budgets(client: TestClient, clock: Clock) -> None:
    response = client.post(ENDPOINT, content=b"", headers=URLENCODED_HEADERS)

    assert response.status_code == 422
    assert spent(client, Limiter.IP) == 1
    assert spent(client, Limiter.ENDPOINT) == 1


def test_an_inactive_endpoint_still_spends_both_budgets(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    a disabled endpoint must not become a free target either
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(seed_endpoint=False)
    create_endpoint(client, "contact-form", is_active=False)

    assert submit(client) == 409

    assert spent(client, Limiter.IP) == 1
    assert spent(client, Limiter.ENDPOINT) == 1


# --- idempotency -------------------------------------------------------------


def key(value: str = "a") -> dict[str, str]:
    """
    build an idempotency header long enough to satisfy the key rules
    :param value: the character the key is built from
    :returns: an ``Idempotency-Key`` header
    """
    return {"Idempotency-Key": value * 32}


def test_a_replay_the_limiter_allows_still_returns_the_original_submission(
    client: TestClient, clock: Clock
) -> None:
    first = client.post(ENDPOINT, data=FORM, headers=key()).json()

    second = client.post(ENDPOINT, data=FORM, headers=key()).json()

    assert second["submission_id"] == first["submission_id"]
    assert second["idempotent_replay"] is True


def test_a_replay_spends_the_budget_like_any_other_attempt(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    otherwise one leaked key would be an unlimited way past the limits
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=2)

    assert client.post(ENDPOINT, data=FORM, headers=key()).status_code == 202
    assert client.post(ENDPOINT, data=FORM, headers=key()).status_code == 202
    assert client.post(ENDPOINT, data=FORM, headers=key()).status_code == 429


def test_a_refused_retry_changes_nothing_that_was_already_stored(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    a 429 is a refusal to do work, not a partial one
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(seed_endpoint=False, rate_limit_ip_requests=1)
    create_endpoint(client, "contact-form", webhook_url="https://example.com/hooks")
    stored = client.post(ENDPOINT, data=FORM, headers=key()).json()

    assert client.post(ENDPOINT, data=FORM, headers=key()).status_code == 429

    with open_session(client) as session:
        submissions = list(session.scalars(select(models.Submission)))
        deliveries = list(session.scalars(select(models.WebhookDelivery)))
    assert [row.id for row in submissions] == [stored["submission_id"]]
    assert [row.submission_id for row in deliveries] == [stored["submission_id"]]
    assert deliveries[0].attempts == 0


def test_a_conflicting_key_is_still_a_conflict_when_the_limiter_allows_it(
    client: TestClient, clock: Clock
) -> None:
    client.post(ENDPOINT, data=FORM, headers=key())

    response = client.post(ENDPOINT, data={"email": "other@example.com"}, headers=key())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


# --- routes the form limits must not touch -----------------------------------


def test_management_routes_are_not_bound_by_the_form_limits(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    ingestion traffic must not be able to lock an operator out of their own service
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=1)
    submit(client)
    assert submit(client) == 429

    assert [client.get("/endpoints").status_code for _ in range(5)] == [200] * 5
    assert client.get("/deliveries").status_code == 200
    assert create_endpoint(client, "another-form")["id"] == "another-form"


def test_health_is_unaffected(make_client: ClientFactory, clock: Clock) -> None:
    client = make_client(rate_limit_ip_requests=1)
    submit(client)
    assert submit(client) == 429

    assert [client.get("/health").status_code for _ in range(5)] == [200] * 5


def test_health_spends_nothing(client: TestClient, clock: Clock) -> None:
    for _ in range(5):
        client.get("/health")

    assert counters(client) == []


# --- cleanup -----------------------------------------------------------------


def test_old_windows_can_be_removed_without_touching_the_current_one(
    client: TestClient,
) -> None:
    """
    claiming these rows are harmless would be untrue, so they have to be removable
    :param client: test client whose app holds the default endpoint
    """
    subject = "a" * 64
    stale = NOON - timedelta(hours=1)
    with open_session(client) as session:
        for start in (stale, NOON):
            storage.consume_rate_limit(
                session, limiter=Limiter.IP, subject=subject, window_start=start
            )

        removed = storage.delete_expired_rate_limit_counters(
            session, before=NOON - timedelta(minutes=5)
        )

    assert removed == 1
    assert [row.window_start for row in counters(client)] == [NOON]


def test_a_submission_sweeps_old_windows_when_it_draws_the_short_straw(
    make_client: ClientFactory, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    the sweep is wired into the request path, not only available to be called
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    :param monkeypatch: pytest fixture used to make the sweep certain rather than rare
    """
    client = make_client(rate_limit_ip_window_seconds=60, rate_limit_endpoint_window_seconds=60)
    with open_session(client) as session:
        storage.consume_rate_limit(
            session,
            limiter=Limiter.IP,
            subject="a" * 64,
            window_start=NOON - timedelta(hours=1),
        )

    # A sweep happens on a small fraction of attempts, so a test that wants to see
    # one has to stop it being a coin toss.
    monkeypatch.setattr("hymical_forms.api.submissions.random.random", lambda: 0.0)
    assert submit(client) == 202

    windows = {row.window_start for row in counters(client)}
    assert windows == {window_start(NOON, 60)}, "the sweep took a window still in use"


def test_spending_a_budget_reports_a_rising_count(client: TestClient) -> None:
    with open_session(client) as session:
        counts = [
            storage.consume_rate_limit(
                session, limiter=Limiter.ENDPOINT, subject="contact-form", window_start=NOON
            )
            for _ in range(4)
        ]

    assert counts == [1, 2, 3, 4]


def test_the_two_limiters_do_not_share_a_counter(client: TestClient) -> None:
    """
    a subject in one limiter must never be the same row as the same string in the other
    :param client: test client whose app holds the default endpoint
    """
    with open_session(client) as session:
        first = storage.consume_rate_limit(
            session, limiter=Limiter.IP, subject="shared", window_start=NOON
        )
        second = storage.consume_rate_limit(
            session, limiter=Limiter.ENDPOINT, subject="shared", window_start=NOON
        )

    assert (first, second) == (1, 1)
    assert len(counters(client)) == 2


# --- the client address trust model ------------------------------------------


def test_a_forwarding_header_is_ignored_by_default(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    a client that could pick its own bucket would have no rate limit at all
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(rate_limit_ip_requests=1)

    assert submit(client, headers=forwarded("203.0.113.1")) == 202
    assert submit(client, headers=forwarded("203.0.113.2")) == 429


def test_a_trusted_hop_counts_the_address_that_proxy_saw(
    make_client: ClientFactory, clock: Clock
) -> None:
    """
    everything to the left of your own proxy's entry was written by somebody else
    :param make_client: factory for clients bound to a configured app
    :param clock: the frozen clock the route stamps windows from
    """
    client = make_client(trusted_proxy_hops=1, rate_limit_ip_requests=1)

    assert submit(client, headers=forwarded("1.1.1.1, 203.0.113.9")) == 202
    # A different forged prefix, the same real client: still one bucket.
    assert submit(client, headers=forwarded("2.2.2.2, 203.0.113.9")) == 429
    assert submit(client, headers=forwarded("2.2.2.2, 203.0.113.8")) == 202


def test_a_trusted_hop_falls_back_to_the_peer_without_a_header(
    make_client: ClientFactory, clock: Clock
) -> None:
    client = make_client(trusted_proxy_hops=1, rate_limit_ip_requests=1)

    assert submit(client) == 202
    assert submit(client) == 429


@pytest.mark.parametrize(
    ("peer", "forwarded_for", "hops", "expected"),
    [
        ("198.51.100.4", "203.0.113.1", 0, "198.51.100.4"),
        ("198.51.100.4", None, 0, "198.51.100.4"),
        ("198.51.100.4", "203.0.113.1", 1, "203.0.113.1"),
        ("198.51.100.4", "1.1.1.1, 203.0.113.1", 1, "203.0.113.1"),
        ("198.51.100.4", " 1.1.1.1 , 203.0.113.1 ", 1, "203.0.113.1"),
        ("198.51.100.4", "1.1.1.1, 203.0.113.1", 2, "1.1.1.1"),
        # A chain shorter than the operator described is not the chain they
        # described, so it is discarded rather than half believed.
        ("198.51.100.4", "203.0.113.1", 2, "198.51.100.4"),
        ("198.51.100.4", "", 1, "198.51.100.4"),
        (None, None, 0, UNKNOWN_CLIENT),
        (None, "203.0.113.1", 0, UNKNOWN_CLIENT),
    ],
)
def test_the_client_address_is_resolved_from_the_declared_trust_model(
    peer: str | None, forwarded_for: str | None, hops: int, expected: str
) -> None:
    resolved = client_address(peer=peer, forwarded_for=forwarded_for, trusted_proxy_hops=hops)

    assert resolved == expected


# --- the rules underneath ----------------------------------------------------


def test_a_window_is_floored_against_the_epoch() -> None:
    """
    every process has to derive the same boundary or they are not sharing a counter
    """
    start = window_start(datetime(2026, 8, 24, 12, 0, 59, 999999, tzinfo=UTC), 60)

    assert start == datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    assert window_start(datetime(2026, 8, 24, 12, 1, tzinfo=UTC), 60) == datetime(
        2026, 8, 24, 12, 1, tzinfo=UTC
    )


def test_the_wait_is_rounded_up_to_whole_seconds() -> None:
    start = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    remaining = seconds_until_window_ends(
        datetime(2026, 8, 24, 12, 0, 30, 500000, tzinfo=UTC), start, 60
    )

    # 29.5 seconds are left, and answering 29 would invite a retry the same window
    # is still going to refuse.
    assert remaining == 30


def test_the_wait_is_never_shorter_than_a_second() -> None:
    start = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    remaining = seconds_until_window_ends(
        datetime(2026, 8, 24, 12, 0, 59, 999999, tzinfo=UTC), start, 60
    )

    assert remaining == 1


def test_a_hashed_address_does_not_contain_the_address() -> None:
    subject = ip_subject("198.51.100.4", None)

    assert "198.51.100.4" not in subject
    assert len(subject) == 64


def test_the_digest_of_an_address_is_stable() -> None:
    """
    two processes must agree on the subject or they are counting different things
    """
    assert ip_subject("198.51.100.4", None) == ip_subject("198.51.100.4", None)
    assert ip_subject("198.51.100.4", "s" * 32) == ip_subject("198.51.100.4", "s" * 32)


def test_a_secret_changes_what_an_address_digests_to() -> None:
    plain = ip_subject("198.51.100.4", None)
    keyed = ip_subject("198.51.100.4", "s" * 32)

    assert plain != keyed
    assert keyed != ip_subject("198.51.100.4", "t" * 32)


def test_different_addresses_digest_differently() -> None:
    assert ip_subject("198.51.100.4", None) != ip_subject("198.51.100.5", None)
