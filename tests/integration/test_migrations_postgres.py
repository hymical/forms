"""
migrations against a real PostgreSQL database

Each test here owns a database of its own, created and dropped around it, so
migrating from genuinely nothing is what is being tested rather than migrating
from whatever a previous test happened to leave behind.
"""

from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from hymical_forms.db import create_engine_from_url
from hymical_forms.models import Base
from hymical_forms.schema import alembic_config, current_revision, head_revision
from integration.support import temporary_database

EXPECTED_TABLES = {"endpoints", "submissions", "webhook_deliveries", "delivery_attempts"}


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
        "ck_endpoints_webhook_configuration",
        "ck_submissions_idempotency_identity",
        "ck_webhook_deliveries_completion",
        "fk_submissions_endpoint_id_endpoints",
        "fk_webhook_deliveries_submission_id_submissions",
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
