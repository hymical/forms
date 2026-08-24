"""Health endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from hymical_forms import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness report for a Hymical Forms process."""

    status: Literal["ok"]
    service: str
    version: str


@router.get("/health", summary="Report process health")
async def health() -> HealthResponse:
    """Report that the API process is running and able to serve requests.

    This is a liveness signal only. Hymical Forms has no external dependencies
    yet, so there is nothing to distinguish readiness from liveness; a separate
    readiness endpoint will arrive with persistence.
    """
    return HealthResponse(status="ok", service="hymical-forms", version=__version__)
