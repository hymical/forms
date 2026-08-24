"""
the Alembic environment

The schema is taken from the application's SQLAlchemy metadata rather than
declared a second time here, and the database URL is read through the same
Settings object the application uses. Neither this file nor alembic.ini holds
credentials.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from hymical_forms.config import Settings
from hymical_forms.db import create_engine_from_url
from hymical_forms.models import Base

target_metadata = Base.metadata


def database_url() -> str:
    """
    work out which database this invocation should migrate
    :returns: the SQLAlchemy database URL to run against
    """
    # ``alembic -x database_url=...`` wins, so an operator can migrate a database
    # other than the one this shell is configured for without exporting anything.
    override = context.get_x_argument(as_dictionary=True).get("database_url")
    if override:
        return override

    # Then a URL handed over programmatically, which is how the tests point a
    # migration run at a throwaway database.
    from_caller = context.config.attributes.get("database_url")
    if from_caller:
        return str(from_caller)

    return Settings().database_url


def run_migrations_offline() -> None:
    """
    emit the migration SQL without connecting to anything
    """
    # Useful for handing a reviewable script to whoever owns the production
    # database, rather than letting a deploy apply DDL unseen.
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    apply the migrations against a live database
    """
    # The application's own engine builder, so SQLite gets the same connection
    # arguments and foreign key enforcement it gets at runtime.
    engine = create_engine_from_url(database_url())
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


def _run(connection: Connection) -> None:
    """
    configure Alembic against an open connection and apply the migrations
    :param connection: the connection to migrate through
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place, so any future migration that
        # changes a column has to be rewritten as a table copy. Batch mode does
        # that automatically, and is inert on PostgreSQL.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
