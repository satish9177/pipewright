"""
Tests for health endpoint database connectivity.
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app

pytestmark = pytest.mark.unit


def test_health_checks_database_connectivity():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


def test_health_returns_503_when_database_unavailable(monkeypatch):
    class BrokenEngine:
        def connect(self):
            raise RuntimeError("db down")

    monkeypatch.setattr("backend.main.engine", BrokenEngine())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 503
    assert "Database unavailable" in response.json()["detail"]
