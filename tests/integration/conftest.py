"""
fixtures for the PostgreSQL integration suite

These tests exist for behaviour SQLite cannot model honestly: real row locking,
real constraint enforcement, and real concurrent worker sessions. They are
skipped unless ``HYMICAL_TEST_POSTGRES_URL`` names a database they may freely
destroy, so the ordinary ``pytest`` run stays fast and needs no services.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms.app import create_app
from hymical_forms.db import create_engine_from_url
from hymical_forms.models import Base
from hymical_forms.schema import alembic_config
from integration.support import (
    POSTGRES_URL_VARIABLE,
    IsolatedSettings,
    drop_everything,
    seed_management_key,
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """
    mark everything in this directory as a PostgreSQL test
    :param items: the collected tests, modified in place
    """
    # Applied here rather than decorated onto every test, so the marker cannot be
    # forgotten on a new one.
    for item in items:
        item.add_marker(pytest.mark.postgres)


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """
    read the URL of a PostgreSQL database these tests may destroy
    :returns: the database URL to run against
    """
    url = os.environ.get(POSTGRES_URL_VARIABLE)
    if not url:
        pytest.skip(f"set {POSTGRES_URL_VARIABLE} to run the PostgreSQL integration suite")
    return url


@pytest.fixture(scope="session")
def migrated_engine(postgres_url: str) -> Iterator[Engine]:
    """
    provide an engine on a database migrated to head by Alembic
    :param postgres_url: the database URL to run against
    :returns: an iterator yielding the engine, disposed when the session ends
    """
    # The schema is built the way production builds it, by running the
    # migrations, so this suite is also a standing test that they work.
    engine = create_engine_from_url(postgres_url)
    drop_everything(engine)
    command.upgrade(alembic_config(postgres_url), "head")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(migrated_engine: Engine) -> None:
    """
    empty every table before each test
    :param migrated_engine: the engine whose database should be emptied
    """
    # Before rather than after, so a test that dies part way through cannot leave
    # rows that poison whatever runs next.
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with migrated_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def sessions(migrated_engine: Engine) -> sessionmaker[Session]:
    """
    provide a session factory that hands out independent connections
    :param migrated_engine: the engine sessions should be bound to
    :returns: a session factory
    """
    # No StaticPool here: each session takes its own connection, which is what
    # makes two sessions genuinely two workers.
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


@pytest.fixture
def pg_client(
    postgres_url: str, migrated_engine: Engine, sessions: sessionmaker[Session]
) -> Iterator[TestClient]:
    """
    provide an API client backed by the migrated PostgreSQL database
    :param postgres_url: the database URL to run against
    :param migrated_engine: unused, but forces the schema to exist first
    :param sessions: session factory used to mint the client's management key
    :returns: an iterator yielding a test client
    """
    app = create_app(
        IsolatedSettings(database_url=postgres_url, allow_private_webhook_targets=True)
    )
    # Issued after ``clean_database`` has truncated everything, so the key exists
    # for exactly the test that is about to run.
    with sessions() as session:
        key = seed_management_key(session)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {key}"
        yield client
