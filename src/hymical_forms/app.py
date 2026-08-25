"""
application assembly
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hymical_forms import __version__
from hymical_forms.api import deliveries, endpoints, health, submission_management, submissions
from hymical_forms.config import Settings
from hymical_forms.db import create_engine_from_url, create_session_factory
from hymical_forms.errors import register_exception_handlers
from hymical_forms.middleware import BodySizeLimitMiddleware
from hymical_forms.schema import verify_schema

DESCRIPTION = """\
Hymical Forms accepts HTML form submissions over HTTP so that developers do not
have to run a form backend of their own.

Submissions are parsed, validated, and stored against a registered endpoint
together with the durable obligation to deliver them. A separate worker process
performs the webhook delivery and retries it.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    check the schema on startup and release the connection pool on shutdown
    :param app: the application starting up
    :returns: an async context manager wrapping the application's serving life
    """
    # The application never migrates. It reaches the database, confirms the
    # schema is the revision this build was written against, and refuses to serve
    # if it is not. Migrating here instead would apply DDL nobody reviewed, at a
    # moment nobody chose, races every other replica starting at the same time,
    # and would hide the mismatch this check is meant to surface.
    verify_schema(app.state.engine)
    yield
    app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    build a hymical forms application
    :param settings: configuration to use, or None to load it from the environment
    :returns: the configured FastAPI application
    """
    # Settings, the engine and the session factory are attached to ``app.state``
    # rather than read from module-level singletons, so a test (or a future
    # multi-tenant host) can run several differently configured applications in
    # one process, each with its own database.
    settings = settings or Settings()

    app = FastAPI(
        title="Hymical Forms",
        summary="Reliable form ingestion and webhook delivery for developers.",
        description=DESCRIPTION,
        version=__version__,
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        lifespan=lifespan,
    )
    engine = create_engine_from_url(settings.database_url)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    # The API process holds no outbound HTTP client. Webhook delivery belongs to
    # the worker, and having nothing to send with is the plainest way to keep the
    # ingestion path free of network calls.

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(endpoints.router)
    app.include_router(deliveries.router)
    app.include_router(submission_management.router)
    app.include_router(submissions.router)

    return app
