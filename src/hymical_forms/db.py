"""
database engine, session, and schema lifecycle
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from hymical_forms.models import Base


def create_engine_from_url(url: str) -> Engine:
    """
    build the engine for a database URL
    :param url: SQLAlchemy database URL, such as ``postgresql+psycopg://.../forms``
    :returns: an engine configured for that backend
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url)

    # SQLite backs the test suite and local experimentation, never production.
    # Requests are served from a thread pool so a connection is not pinned to the
    # thread that opened it, and an in-memory database only exists for as long as
    # its single connection is held, which is what StaticPool guarantees.
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if parsed.database in (None, "", ":memory:"):
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    """
    switch on SQLite foreign key enforcement, which is off by default
    :param dbapi_connection: the freshly opened DBAPI connection
    :param connection_record: the pool's bookkeeping record, unused
    """
    # Without this, SQLite accepts rows PostgreSQL would reject, and the test
    # suite would stop being a faithful stand-in for the real database.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """
    build the session factory an application will serve requests from
    :param engine: the engine sessions should be bound to
    :returns: a configured session factory
    """
    # ``expire_on_commit=False`` keeps loaded values readable after a commit, so
    # building a response out of a just-committed row costs no extra query.
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """
    create any tables that do not exist yet
    :param engine: the engine whose database should hold the schema
    """
    # There is no migration framework yet, so this is the whole schema story: it
    # creates missing tables and never alters existing ones.
    Base.metadata.create_all(engine)


def get_session(request: Request) -> Iterator[Session]:
    """
    provide the session a request should do its database work through
    :param request: the request being served
    :returns: an iterator yielding one session, closed when the request ends
    """
    # Closing a session rolls back whatever was not committed, so a handler that
    # raises part way through leaves nothing behind.
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
