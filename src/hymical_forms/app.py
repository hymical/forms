"""
application assembly
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from hymical_forms import __version__
from hymical_forms.api import endpoints, health, submissions
from hymical_forms.config import Settings
from hymical_forms.db import create_engine_from_url, create_session_factory, init_db
from hymical_forms.delivery import create_webhook_client
from hymical_forms.errors import register_exception_handlers
from hymical_forms.middleware import BodySizeLimitMiddleware

DESCRIPTION = """\
Hymical Forms accepts HTML form submissions over HTTP so that developers do not
have to run a form backend of their own.

Submissions are parsed, validated and stored against a registered endpoint, then
delivered once to that endpoint's webhook if it has one. There are no automatic
retries yet.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    prepare the schema on startup and release the connection pool on shutdown
    :param app: the application starting up
    :returns: an async context manager wrapping the application's serving life
    """
    # There is no migration framework yet, so creating missing tables at startup
    # is the whole schema story. It is safe to repeat and never alters a table
    # that already exists, which also means a changed column needs manual work.
    init_db(app.state.engine)
    yield
    await app.state.webhook_client.aclose()
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
    # One outbound client for the process, so webhook connections are pooled
    # rather than renegotiated per submission. Closed again in the lifespan.
    app.state.webhook_client = create_webhook_client(settings)

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(endpoints.router)
    app.include_router(submissions.router)

    return app
