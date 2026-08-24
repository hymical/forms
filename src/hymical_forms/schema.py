"""
the boundary between the application and Alembic

Alembic owns the production schema. Nothing here ever migrates a database: the
application only asks whether the schema it was given is the one it was built
for, and says so plainly when it is not. Applying migrations is an operator
action, run deliberately, at a time the operator chooses.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from hymical_forms.models import Base

MIGRATIONS_PATH = Path(__file__).resolve().parent / "migrations"


class SchemaNotReady(RuntimeError):
    """
    raised when the database is not at the revision this build expects
    """


def alembic_config(database_url: str | None = None) -> Config:
    """
    build an Alembic config pointing at the migrations shipped with this package
    :param database_url: database to target, or None to let env.py resolve it
    :returns: a config usable with Alembic's programmatic API
    """
    # Built in code rather than read from alembic.ini so that this works from an
    # installed wheel, where the repository's ini file is not present.
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    if database_url is not None:
        # Passed through attributes rather than as ``sqlalchemy.url``, so a
        # password containing a percent sign is never mangled by ini
        # interpolation on its way to the database.
        config.attributes["database_url"] = database_url
    return config


@lru_cache(maxsize=1)
def head_revision() -> str:
    """
    read the newest revision in the migrations shipped with this build
    :returns: the head revision identifier
    """
    # Cached because the migration scripts cannot change while the process runs,
    # and this would otherwise scan a directory on every application startup.
    script = ScriptDirectory(str(MIGRATIONS_PATH))
    head = script.get_current_head()
    if head is None:
        raise SchemaNotReady("this build ships no migrations")
    return head


def current_revision(engine: Engine) -> str | None:
    """
    read the revision a database is currently at
    :param engine: the engine to inspect through
    :returns: the stored revision, or None if the database has never been migrated
    """
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def verify_schema(engine: Engine) -> None:
    """
    check that the database is reachable and at the revision this build expects
    :param engine: the engine to verify
    :raises SchemaNotReady: if the database has no schema or is at another revision
    """
    # Deliberately a check and not a migration. An application that quietly
    # altered the schema it found would make deploys unreviewable and would race
    # every other replica starting at the same moment.
    expected = head_revision()
    found = current_revision(engine)

    if found is None:
        raise SchemaNotReady(
            "the database has no schema. Run 'alembic upgrade head' before starting."
        )
    if found != expected:
        raise SchemaNotReady(
            f"the database is at migration {found!r} but this build expects {expected!r}. "
            "Run 'alembic upgrade head' before starting."
        )


def create_all(engine: Engine) -> None:
    """
    build the schema straight from the models and record it as fully migrated
    :param engine: the engine whose database should hold the schema
    """
    # For tests and throwaway databases only. It is much faster than replaying
    # migrations for every test, and the stamp is what lets the application's
    # startup check accept the result. A PostgreSQL test asserts that what this
    # produces and what the migrations produce are the same schema, so the
    # shortcut cannot quietly drift away from the real one.
    Base.metadata.create_all(engine)
    stamp_head(engine)


def stamp_head(engine: Engine) -> None:
    """
    record a database as being at the newest revision without running migrations
    :param engine: the engine whose database should be stamped
    """
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        context.stamp(ScriptDirectory(str(MIGRATIONS_PATH)), head_revision())
