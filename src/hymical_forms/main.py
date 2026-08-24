"""ASGI entrypoint.

Run with::

    uvicorn hymical_forms.main:app
"""

from __future__ import annotations

from hymical_forms.app import create_app

app = create_app()
