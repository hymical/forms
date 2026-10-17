"""
health endpoint
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from hymical_forms import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """
    liveness report for a hymical forms process
    """

    status: Literal["ok"]
    service: str
    version: str


@router.get("/health", summary="Report process health")
async def health() -> HealthResponse:
    """
    report that the api process is running and able to serve requests
    :returns: a liveness payload naming the service and its version
    """
    # Liveness only, and deliberately free of database access, so it keeps
    # answering while PostgreSQL is down and a supervisor does not restart a
    # healthy process for an outage it cannot fix. The schema and reachability
    # check happens once, at startup (see schema.verify_schema). There is
    # deliberately no readiness route for v1.0: it would widen the public surface
    # beyond this route and POST /f/{endpoint_id}.
    return HealthResponse(status="ok", service="hymical-forms", version=__version__)
