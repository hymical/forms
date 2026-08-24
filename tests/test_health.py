"""Health endpoint behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hymical_forms import __version__


def test_health_reports_a_running_process(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "hymical-forms",
        "version": __version__,
    }
