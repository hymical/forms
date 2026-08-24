"""
helpers for the PostgreSQL integration suite

Kept out of ``conftest.py`` deliberately. Both ``tests/`` and
``tests/integration/`` end up on sys.path, so a plain ``import conftest`` from a
test in this directory could resolve to either file depending on collection
order. This module's name is unambiguous.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from pydantic_settings import SettingsConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from hymical_forms import apikeys, models, storage
from hymical_forms.config import Settings
from hymical_forms.db import create_engine_from_url
from hymical_forms.models import utcnow
from hymical_forms.webhooks import DeliveryState

POSTGRES_URL_VARIABLE = "HYMICAL_TEST_POSTGRES_URL"


class IsolatedSettings(Settings):
    """
    settings that ignore a local ``.env``, so a developer's file cannot skew a run
    """

    model_config = SettingsConfigDict(env_file=None)


@contextmanager
def temporary_database(postgres_url: str) -> Iterator[str]:
    """
    create an empty database for one test and drop it afterwards
    :param postgres_url: a URL on the server the new database should live on
    :returns: a context manager yielding the new database's URL
    """
    # Migration tests need to own a whole database, so they get one rather than
    # fighting the shared schema the rest of the suite runs against.
    url = make_url(postgres_url)
    name = f"hymical_test_{uuid.uuid4().hex[:12]}"
    # ``str(URL)`` masks the password, so rendering has to be explicit or the
    # connection arrives with a literal "***" and is refused.
    admin = create_engine_from_url(
        url.set(database="postgres").render_as_string(hide_password=False)
    )
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield url.set(database=name).render_as_string(hide_password=False)
        finally:
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def drop_everything(engine: Engine) -> None:
    """
    return a database to being empty, whatever a previous run left in it
    :param engine: the engine whose database should be emptied
    """
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def seed_management_key(session: Session, name: str = "integration-tests") -> str:
    """
    mint a management key into the database, the way the operator CLI does
    :param session: the session to insert through
    :param name: human-readable label for the key
    :returns: the full credential, which exists only here and in the caller
    """
    generated = apikeys.new_management_key()
    storage.create_management_key(
        session,
        key_id=generated.id,
        name=name,
        display_prefix=generated.display_prefix,
        key_digest=generated.digest,
        now=utcnow(),
    )
    session.commit()
    return generated.key


def seed_endpoint(session: Session, endpoint_id: str = "contact-form") -> models.Endpoint:
    """
    insert an endpoint with a webhook configured
    :param session: the session to insert through
    :param endpoint_id: the identifier to give it
    :returns: the committed endpoint
    """
    endpoint = models.Endpoint(
        id=endpoint_id,
        name="Contact form",
        is_active=True,
        webhook_url="https://example.invalid/hook",
        webhook_secret="whsec_" + "a" * 64,
    )
    session.add(endpoint)
    session.commit()
    return endpoint


def seed_due_deliveries(
    session: Session, count: int, *, now: datetime, endpoint_id: str = "contact-form"
) -> list[str]:
    """
    insert submissions each owing a delivery that is already due
    :param session: the session to insert through
    :param count: how many to create
    :param now: the instant the deliveries should have become due
    :param endpoint_id: the endpoint they belong to
    :returns: the delivery ids, oldest first
    """
    ids: list[str] = []
    for index in range(count):
        submission_id = f"sub_{uuid.uuid4().hex}"
        delivery_id = f"whd_{uuid.uuid4().hex}"
        session.add(
            models.Submission(
                id=submission_id,
                endpoint_id=endpoint_id,
                received_at=now,
                fields={"email": [f"dev{index}@example.com"]},
            )
        )
        session.add(
            models.WebhookDelivery(
                id=delivery_id,
                submission_id=submission_id,
                destination_url="https://example.invalid/hook",
                signing_secret="whsec_" + "a" * 64,
                state=DeliveryState.PENDING,
                attempts=0,
                # Staggered into the past so ordering is deterministic.
                next_attempt_at=now - timedelta(seconds=count - index),
                created_at=now,
            )
        )
        ids.append(delivery_id)
    session.commit()
    return ids
