"""Application assembly."""

from __future__ import annotations

from fastapi import FastAPI

from hymical_forms import __version__
from hymical_forms.api import health, submissions
from hymical_forms.config import Settings
from hymical_forms.errors import register_exception_handlers
from hymical_forms.middleware import BodySizeLimitMiddleware

DESCRIPTION = """\
Hymical Forms accepts HTML form submissions over HTTP so that developers do not
have to run a form backend of their own.

This build implements the ingestion boundary only: submissions are parsed,
validated and acknowledged, but not stored or delivered anywhere.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a Hymical Forms application.

    Settings are attached to ``app.state`` rather than read from a module-level
    singleton, so a test (or a future multi-tenant host) can run several
    differently configured applications in one process.
    """
    settings = settings or Settings()

    app = FastAPI(
        title="Hymical Forms",
        summary="Reliable form ingestion and webhook delivery for developers.",
        description=DESCRIPTION,
        version=__version__,
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
    )
    app.state.settings = settings

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(submissions.router)

    return app
