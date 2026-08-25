"""
migrations against a real PostgreSQL database

Each test here owns a database of its own, created and dropped around it, so
migrating from genuinely nothing is what is being tested rather than migrating
from whatever a previous test happened to leave behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, inspect, text

from hymical_forms.db import create_engine_from_url
from hymical_forms.models import Base
from hymical_forms.schema import alembic_config, current_revision, head_revision
from integration.support import temporary_database

BASELINE_TABLES = {"endpoints", "submissions", "webhook_deliveries", "delivery_attempts"}
EXPECTED_TABLES = BASELINE_TABLES | {"management_api_keys", "rate_limit_counters"}

# Representative interval 6 data: an endpoint with a webhook, a submission sent
# with an idempotency key, the delivery it owes, and one recorded attempt.
SEEDED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
SEEDED_ENDPOINT = "contact-form"
SEEDED_SUBMISSION = "sub_11111111111111111111111111111111"
SEEDED_DELIVERY = "whd_22222222222222222222222222222222"
SEEDED_ATTEMPT = "att_33333333333333333333333333333333"


def test_an_empty_database_upgrades_to_head(postgres_url: str) -> None:
    with temporary_database(postgres_url) as url:
        engine = create_engine_from_url(url)
        try:
            assert current_revision(engine) is None

            command.upgrade(alembic_config(url), "head")

            assert current_revision(engine) == head_revision()
            assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
        finally:
            engine.dispose()


def test_the_migrated_schema_matches_the_models(postgres_url: str) -> None:
    """
    the migration and the models must not be allowed to drift apart
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    # This is what makes it safe for the fast suite to build its schema with
    # create_all instead of replaying migrations: the two are the same schema.
    with temporary_database(postgres_url) as url:
        command.upgrade(alembic_config(url), "head")
        engine = create_engine_from_url(url)
        try:
            with engine.connect() as connection:
                difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        finally:
            engine.dispose()

    assert difference == [], f"migrated schema differs from the models: {difference}"


def test_the_migration_creates_the_constraints_the_application_relies_on(
    postgres_url: str,
) -> None:
    with temporary_database(postgres_url) as url:
        command.upgrade(alembic_config(url), "head")
        engine = create_engine_from_url(url)
        try:
            with engine.connect() as connection:
                names = set(
                    connection.scalars(
                        text(
                            "select conname from pg_constraint c "
                            "join pg_class t on t.oid = c.conrelid "
                            "where t.relnamespace = 'public'::regnamespace"
                        )
                    )
                )
        finally:
            engine.dispose()

    assert {
        "uq_submissions_endpoint_idempotency_key",
        "uq_webhook_deliveries_submission",
        "uq_management_api_keys_key_digest",
        "pk_rate_limit_counters",
        "ck_endpoints_webhook_configuration",
        "ck_submissions_idempotency_identity",
        "ck_webhook_deliveries_completion",
        "fk_submissions_endpoint_id_endpoints",
        "fk_webhook_deliveries_submission_id_submissions",
        "fk_webhook_deliveries_endpoint_id_endpoints",
        "fk_delivery_attempts_delivery_id_webhook_deliveries",
        "fk_delivery_attempts_submission_id_submissions",
    } <= names


def test_timestamps_are_stored_with_a_timezone(postgres_url: str) -> None:
    """
    the delivery queue compares instants, so a naive column would be a real bug
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with temporary_database(postgres_url) as url:
        command.upgrade(alembic_config(url), "head")
        engine = create_engine_from_url(url)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "select column_name, data_type from information_schema.columns "
                        "where table_name = 'webhook_deliveries'"
                    )
                ).all()
                types = {str(row[0]): str(row[1]) for row in rows}
        finally:
            engine.dispose()

    assert types["next_attempt_at"] == "timestamp with time zone"
    assert types["claim_expires_at"] == "timestamp with time zone"
    assert types["completed_at"] == "timestamp with time zone"


def test_the_migration_round_trips(postgres_url: str) -> None:
    """
    downgrading to base and upgrading again must leave the same schema
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with temporary_database(postgres_url) as url:
        config = alembic_config(url)
        engine = create_engine_from_url(url)
        try:
            command.upgrade(config, "head")

            command.downgrade(config, "base")
            remaining = set(inspect(engine).get_table_names())
            assert remaining & EXPECTED_TABLES == set()

            command.upgrade(config, "head")
            assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
            assert current_revision(engine) == head_revision()

            with engine.connect() as connection:
                difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert difference == []
        finally:
            engine.dispose()


# --- upgrading a database that already holds data ----------------------------
#
# Interval 6 built the migration machinery but only ever ran it against an empty
# database. These tests are the first time an upgrade has had to preserve data it
# cares about, which is the property an operator is actually relying on.


def test_upgrading_a_populated_baseline_preserves_its_data(postgres_url: str) -> None:
    """
    an existing endpoint, submission, delivery and attempt must survive 0001 to 0002
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)

        command.upgrade(config, "0002")

        assert current_revision(engine) == "0002"
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)


def test_upgrading_a_populated_baseline_adds_the_key_table(postgres_url: str) -> None:
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        assert "management_api_keys" not in set(inspect(engine).get_table_names())

        command.upgrade(config, "0002")

        assert "management_api_keys" in set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(text("select count(*) from management_api_keys")) == 0


def test_downgrading_from_0002_leaves_the_baseline_data_alone(postgres_url: str) -> None:
    """
    the downgrade must remove only what 0002 added
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0002")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into management_api_keys "
                    "(id, name, display_prefix, key_digest, created_at) "
                    "values ('mk_test', 'operator', 'hym_live_abcdefgh', :digest, :now)"
                ),
                {"digest": "d" * 64, "now": SEEDED_AT},
            )

        command.downgrade(config, "0001")

        assert current_revision(engine) == "0001"
        assert "management_api_keys" not in set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)


def test_a_populated_database_upgraded_again_still_matches_the_models(postgres_url: str) -> None:
    """
    the whole round trip on real data must end with zero migration and model drift
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)

        command.upgrade(config, "0002")
        command.downgrade(config, "0001")
        command.upgrade(config, "head")

        assert current_revision(engine) == head_revision()
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert difference == [], f"migrated schema differs from the models: {difference}"


# --- the retry cycle counter 0003 added --------------------------------------
#
# 0003 is the first revision that changes an existing table rather than adding a
# new one, so an upgrade has to preserve rows it is also rewriting.


def test_upgrading_a_populated_0002_adds_the_cycle_counter(postgres_url: str) -> None:
    """
    a delivery that has never been replayed must come out with its whole history as its cycle
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0002")

        command.upgrade(config, "0003")

        assert current_revision(engine) == "0003"
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            row = connection.execute(
                text("select attempts, cycle_attempts from webhook_deliveries where id = :id"),
                {"id": SEEDED_DELIVERY},
            ).one()
        assert row.attempts == 1
        assert row.cycle_attempts == 1


def test_the_cycle_counter_is_not_nullable(postgres_url: str) -> None:
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0003")

        with engine.connect() as connection:
            nullable = connection.scalar(
                text(
                    "select is_nullable from information_schema.columns "
                    "where table_name = 'webhook_deliveries' and column_name = 'cycle_attempts'"
                )
            )

    assert nullable == "NO"


def test_downgrading_from_0003_leaves_the_delivery_data_alone(postgres_url: str) -> None:
    """
    the downgrade must remove the column 0003 added and nothing else
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0003")

        command.downgrade(config, "0002")

        assert current_revision(engine) == "0002"
        columns = {column["name"] for column in inspect(engine).get_columns("webhook_deliveries")}
        assert "cycle_attempts" not in columns
        assert "attempts" in columns
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)


def test_a_populated_0002_survives_the_whole_round_trip(postgres_url: str) -> None:
    """
    populated 0002 to 0003 and back and forward to head must end with zero drift
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0002")

        command.upgrade(config, "0003")
        command.downgrade(config, "0002")
        command.upgrade(config, "head")

        assert current_revision(engine) == head_revision()
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            assert (
                connection.scalar(
                    text("select cycle_attempts from webhook_deliveries where id = :id"),
                    {"id": SEEDED_DELIVERY},
                )
                == 1
            )
            difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert difference == [], f"migrated schema differs from the models: {difference}"


# --- the rate limit counters 0004 added --------------------------------------
#
# 0004 adds a table rather than changing one, so what an upgrade has to prove
# here is that everything an operator's database already held is untouched, and
# that the new table arrives empty and ready rather than needing a backfill.


def test_upgrading_a_populated_0003_adds_the_counter_table(postgres_url: str) -> None:
    """
    the rate limit table must arrive without disturbing anything already stored
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0003")
        assert "rate_limit_counters" not in set(inspect(engine).get_table_names())

        command.upgrade(config, "0004")

        assert current_revision(engine) == "0004"
        assert "rate_limit_counters" in set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            assert connection.scalar(text("select count(*) from rate_limit_counters")) == 0


def test_the_counter_table_is_keyed_by_limiter_subject_and_window(postgres_url: str) -> None:
    """
    the upsert conflicts on this key, so the key is what makes the increment atomic
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0004")

        with engine.connect() as connection:
            key = list(
                connection.scalars(
                    text(
                        "select a.attname from pg_index i "
                        "join pg_attribute a on a.attrelid = i.indrelid "
                        "and a.attnum = any(i.indkey) "
                        "where i.indrelid = 'rate_limit_counters'::regclass and i.indisprimary"
                    )
                )
            )
            indexed = set(
                connection.scalars(
                    text("select indexname from pg_indexes where tablename = 'rate_limit_counters'")
                )
            )

    assert set(key) == {"limiter", "subject", "window_start"}
    # Cleanup ranges over the window column without knowing a limiter or a
    # subject, which the primary key cannot answer.
    assert "ix_rate_limit_counters_window_start" in indexed


def test_the_counter_window_is_stored_with_a_timezone(postgres_url: str) -> None:
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0004")

        with engine.connect() as connection:
            data_type = connection.scalar(
                text(
                    "select data_type from information_schema.columns "
                    "where table_name = 'rate_limit_counters' and column_name = 'window_start'"
                )
            )

    assert data_type == "timestamp with time zone"


def test_downgrading_from_0004_leaves_everything_else_alone(postgres_url: str) -> None:
    """
    the downgrade must remove the table 0004 added and nothing else
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0004")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into rate_limit_counters (limiter, subject, window_start, attempts) "
                    "values ('ip', :subject, :now, 3)"
                ),
                {"subject": "e" * 64, "now": SEEDED_AT},
            )

        command.downgrade(config, "0003")

        assert current_revision(engine) == "0003"
        assert "rate_limit_counters" not in set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)


def test_a_populated_0003_survives_the_whole_round_trip(postgres_url: str) -> None:
    """
    populated 0003 to 0004 and back and forward again must end with zero drift
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0003")

        command.upgrade(config, "0004")
        command.downgrade(config, "0003")
        command.upgrade(config, "head")

        assert current_revision(engine) == head_revision()
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            assert (
                connection.scalar(
                    text("select cycle_attempts from webhook_deliveries where id = :id"),
                    {"id": SEEDED_DELIVERY},
                )
                == 1
            )
            difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert difference == [], f"migrated schema differs from the models: {difference}"


# --- the retention schema 0005 introduced ------------------------------------
#
# 0005 is the first revision that loosens a constraint rather than adding one, so
# what it has to prove is that the loosening is deliberate and that the column it
# backfills ends up holding what the join it replaces used to produce.


def test_upgrading_a_populated_0004_backfills_the_delivery_endpoint(postgres_url: str) -> None:
    """
    the column that replaces a join must arrive holding what the join produced
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0004")

        command.upgrade(config, "0005")

        assert current_revision(engine) == "0005"
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            endpoint = connection.scalar(
                text("select endpoint_id from webhook_deliveries where id = :id"),
                {"id": SEEDED_DELIVERY},
            )
        assert endpoint == SEEDED_ENDPOINT


def test_the_delivery_endpoint_is_not_nullable(postgres_url: str) -> None:
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0005")

        with engine.connect() as connection:
            nullable = connection.scalar(
                text(
                    "select is_nullable from information_schema.columns "
                    "where table_name = 'webhook_deliveries' and column_name = 'endpoint_id'"
                )
            )

    assert nullable == "NO"


@pytest.mark.parametrize("table", ["webhook_deliveries", "delivery_attempts"])
def test_the_submission_link_becomes_nullable(postgres_url: str, table: str) -> None:
    """
    a submission cannot be deleted without somewhere for its history to point instead
    :param postgres_url: a URL on the PostgreSQL server to work against
    :param table: the table whose link to the submission is being checked
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0005")

        with engine.connect() as connection:
            nullable = connection.scalar(
                text(
                    "select is_nullable from information_schema.columns "
                    "where table_name = :table and column_name = 'submission_id'"
                ),
                {"table": table},
            )

    assert nullable == "YES"


@pytest.mark.parametrize(
    "constraint",
    [
        "fk_webhook_deliveries_submission_id_submissions",
        "fk_delivery_attempts_submission_id_submissions",
    ],
)
def test_deleting_a_submission_unlinks_history_rather_than_cascading(
    postgres_url: str, constraint: str
) -> None:
    """
    a cascade here would take the operational history with the form content
    :param postgres_url: a URL on the PostgreSQL server to work against
    :param constraint: the foreign key whose delete rule is being checked
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0005")

        with engine.connect() as connection:
            rule = connection.scalar(
                text(
                    "select confdeltype from pg_constraint where conname = :name",
                ),
                {"name": constraint},
            )

    # ``n`` is SET NULL. ``c`` would be CASCADE, which is the mistake this asserts
    # against, and ``a`` would be NO ACTION, which is what 0004 had.
    assert rule == "n"


def test_the_submission_indexes_match_what_management_reads(postgres_url: str) -> None:
    with _database_at_baseline(postgres_url) as (config, engine):
        command.upgrade(config, "0005")

        with engine.connect() as connection:
            indexed = set(
                connection.scalars(
                    text("select indexname from pg_indexes where tablename = 'submissions'")
                )
            )

    assert "ix_submissions_received_at_id" in indexed
    assert "ix_submissions_endpoint_id_received_at_id" in indexed
    # Replaced rather than joined by the composite, whose leftmost column answers
    # the same lookup.
    assert "ix_submissions_endpoint_id" not in indexed


def test_downgrading_from_0005_removes_what_it_added(postgres_url: str) -> None:
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0005")

        command.downgrade(config, "0004")

        assert current_revision(engine) == "0004"
        columns = {column["name"] for column in inspect(engine).get_columns("webhook_deliveries")}
        assert "endpoint_id" not in columns
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)


def test_downgrading_removes_only_the_deliveries_that_lost_their_submission(
    postgres_url: str,
) -> None:
    """
    the older schema cannot hold an unlinked delivery, and everything else must survive
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0005")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into submissions (id, endpoint_id, received_at, fields) values "
                    "(:id, :endpoint, :now, :fields)"
                ),
                {
                    "id": "sub_44444444444444444444444444444444",
                    "endpoint": SEEDED_ENDPOINT,
                    "now": SEEDED_AT,
                    "fields": '{"email": ["gone@example.com"]}',
                },
            )
            connection.execute(
                text(
                    "insert into webhook_deliveries (id, submission_id, endpoint_id, "
                    "destination_url, signing_secret, state, attempts, cycle_attempts, "
                    "next_attempt_at, created_at, completed_at) values "
                    "(:id, :submission, :endpoint, 'https://example.invalid/hook', :secret, "
                    "'delivered', 1, 1, :now, :now, :now)"
                ),
                {
                    "id": "whd_55555555555555555555555555555555",
                    "submission": "sub_44444444444444444444444444444444",
                    "endpoint": SEEDED_ENDPOINT,
                    "now": SEEDED_AT,
                    "secret": "whsec_" + "a" * 64,
                },
            )
            # Retention taking a delivered submission, which is the only way a
            # delivery ever ends up without one.
            connection.execute(
                text("delete from submissions where id = :id"),
                {"id": "sub_44444444444444444444444444444444"},
            )

        command.downgrade(config, "0004")

        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            remaining = set(connection.scalars(text("select id from webhook_deliveries")))
        assert remaining == {SEEDED_DELIVERY}


def test_a_populated_0004_survives_the_whole_round_trip(postgres_url: str) -> None:
    """
    populated 0004 to 0005 and back and forward again must end with zero drift
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    with _database_at_baseline(postgres_url) as (config, engine):
        with engine.begin() as connection:
            _seed_baseline_data(connection)
        command.upgrade(config, "0004")

        command.upgrade(config, "0005")
        command.downgrade(config, "0004")
        command.upgrade(config, "head")

        assert current_revision(engine) == head_revision()
        with engine.connect() as connection:
            _assert_baseline_data_intact(connection)
            assert (
                connection.scalar(
                    text("select endpoint_id from webhook_deliveries where id = :id"),
                    {"id": SEEDED_DELIVERY},
                )
                == SEEDED_ENDPOINT
            )
            difference = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert difference == [], f"migrated schema differs from the models: {difference}"


@contextmanager
def _database_at_baseline(postgres_url: str) -> Iterator[tuple[Config, Engine]]:
    """
    provide a throwaway database migrated to 0001 and nothing further
    :param postgres_url: a URL on the PostgreSQL server to work against
    :returns: a context manager yielding the alembic config and an engine on it
    """
    with temporary_database(postgres_url) as url:
        config = alembic_config(url)
        command.upgrade(config, "0001")
        engine = create_engine_from_url(url)
        try:
            yield config, engine
        finally:
            engine.dispose()


def _seed_baseline_data(connection: Connection) -> None:
    """
    insert one of each interval 6 row, as an operator's database would hold
    :param connection: the connection to insert through
    """
    # Written as SQL rather than through the ORM on purpose. The models describe
    # head, and this data is meant to be what a database at 0001 already contains.
    connection.execute(
        text(
            "insert into endpoints (id, name, is_active, created_at, webhook_url, webhook_secret) "
            "values (:id, 'Contact form', true, :now, 'https://example.invalid/hook', :secret)"
        ),
        {"id": SEEDED_ENDPOINT, "now": SEEDED_AT, "secret": "whsec_" + "a" * 64},
    )
    connection.execute(
        text(
            "insert into submissions "
            "(id, endpoint_id, received_at, fields, idempotency_key, payload_fingerprint) "
            "values (:id, :endpoint, :now, :fields, :key, :fingerprint)"
        ),
        {
            "id": SEEDED_SUBMISSION,
            "endpoint": SEEDED_ENDPOINT,
            "now": SEEDED_AT,
            "fields": '{"email": ["dev@example.com"]}',
            "key": "b8f1c2d4e5a67890b8f1c2d4e5a67890",
            "fingerprint": "f" * 64,
        },
    )
    connection.execute(
        text(
            "insert into webhook_deliveries "
            "(id, submission_id, destination_url, signing_secret, state, attempts, "
            "next_attempt_at, created_at, completed_at) "
            "values (:id, :submission, 'https://example.invalid/hook', :secret, 'delivered', 1, "
            ":now, :now, :now)"
        ),
        {
            "id": SEEDED_DELIVERY,
            "submission": SEEDED_SUBMISSION,
            "now": SEEDED_AT,
            "secret": "whsec_" + "a" * 64,
        },
    )
    connection.execute(
        text(
            "insert into delivery_attempts "
            "(id, delivery_id, submission_id, attempt_number, destination_url, attempted_at, "
            "outcome, response_status) "
            "values (:id, :delivery, :submission, 1, 'https://example.invalid/hook', :now, "
            "'succeeded', 200)"
        ),
        {
            "id": SEEDED_ATTEMPT,
            "delivery": SEEDED_DELIVERY,
            "submission": SEEDED_SUBMISSION,
            "now": SEEDED_AT,
        },
    )


def _assert_baseline_data_intact(connection: Connection) -> None:
    """
    check that every seeded row is still there and still says what it said
    :param connection: the connection to query through
    """
    endpoint = connection.execute(
        text("select name, webhook_url, webhook_secret from endpoints where id = :id"),
        {"id": SEEDED_ENDPOINT},
    ).one()
    assert endpoint.name == "Contact form"
    assert endpoint.webhook_url == "https://example.invalid/hook"
    assert endpoint.webhook_secret == "whsec_" + "a" * 64

    submission = connection.execute(
        text("select endpoint_id, fields, idempotency_key from submissions where id = :id"),
        {"id": SEEDED_SUBMISSION},
    ).one()
    assert submission.endpoint_id == SEEDED_ENDPOINT
    assert submission.fields == {"email": ["dev@example.com"]}
    assert submission.idempotency_key == "b8f1c2d4e5a67890b8f1c2d4e5a67890"

    delivery = connection.execute(
        text("select submission_id, state, attempts from webhook_deliveries where id = :id"),
        {"id": SEEDED_DELIVERY},
    ).one()
    assert delivery.submission_id == SEEDED_SUBMISSION
    assert delivery.state == "delivered"
    assert delivery.attempts == 1

    attempt = connection.execute(
        text("select delivery_id, outcome, response_status from delivery_attempts where id = :id"),
        {"id": SEEDED_ATTEMPT},
    ).one()
    assert attempt.delivery_id == SEEDED_DELIVERY
    assert attempt.outcome == "succeeded"
    assert attempt.response_status == 200
