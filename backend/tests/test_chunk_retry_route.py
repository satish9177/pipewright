"""
test_chunk_retry_route.py
Focused tests for the public human-triggered chunk retry route (#26D3b).
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.pipeline.run_locks import (
    PROJECT_LOCK_CONFLICT_MESSAGE,
    ProjectRepoLockError,
)

pytestmark = pytest.mark.unit


def _client() -> TestClient:
    return TestClient(app)


def _retry_ineligible(reason: str, status_code: int) -> dict:
    return {
        "status": "retry_ineligible",
        "run_id": "run-123",
        "chunk_number": 2,
        "eligible": False,
        "reason": reason,
        "status_code": status_code,
    }


def test_retry_route_requires_failure_report_id_body():
    response = _client().post("/runs/run-123/chunks/2/retry")

    assert response.status_code == 422


def test_retry_route_rejects_blank_failure_report_id():
    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "   "},
    )

    assert response.status_code == 422


def test_retry_route_calls_helper_with_stripped_failure_report_id(monkeypatch):
    called = {}

    async def fake_retry(run_id, chunk_number, failure_report_id):
        called["run_id"] = run_id
        called["chunk_number"] = chunk_number
        called["failure_report_id"] = failure_report_id
        return {
            "status": "awaiting_chunk_approval",
            "run_id": run_id,
            "chunk_number": chunk_number,
            "failure_report_id": "new-frid",
        }

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "  old-frid  "},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_chunk_approval"
    assert called == {
        "run_id": "run-123",
        "chunk_number": 2,
        "failure_report_id": "old-frid",
    }


def test_retry_route_success_returns_http_200(monkeypatch):
    async def fake_retry(run_id, chunk_number, failure_report_id):
        return {
            "status": "awaiting_chunk_approval",
            "run_id": run_id,
            "chunk_number": chunk_number,
            "failure_report_id": "new-frid",
        }

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_chunk_approval"


def test_retry_route_executed_failure_returns_http_200(monkeypatch):
    async def fake_retry(run_id, chunk_number, failure_report_id):
        return {
            "status": "failed",
            "run_id": run_id,
            "failed_chunk": chunk_number,
            "error": "patch still does not apply",
            "failure_report_id": "new-frid",
        }

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_report_id"] == "new-frid"


@pytest.mark.parametrize(
    "reason,status_code",
    [
        ("stale_failure_report_id", 409),
        ("wrong_branch", 409),
        ("dirty_worktree", 409),
        ("missing_or_malformed_report", 422),
        ("disallowed_failure_type", 422),
        ("unmet_dependency", 422),
        ("attempt_cap_exhausted", 422),
    ],
)
def test_retry_route_maps_retry_ineligible_status_code(
    monkeypatch,
    reason,
    status_code,
):
    async def fake_retry(run_id, chunk_number, failure_report_id):
        return _retry_ineligible(reason, status_code)

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert response.status_code == status_code
    assert response.json()["status"] == "retry_ineligible"
    assert response.json()["reason"] == reason
    assert response.json()["status_code"] == status_code


def test_retry_route_maps_project_lock_conflict_to_409(monkeypatch):
    async def fake_retry(run_id, chunk_number, failure_report_id):
        raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_LOCK_CONFLICT_MESSAGE


def test_retry_route_maps_value_error_to_404(monkeypatch):
    async def fake_retry(run_id, chunk_number, failure_report_id):
        raise ValueError("run not found")

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "run not found"


def test_retry_route_maps_generic_error_to_400(monkeypatch):
    async def fake_retry(run_id, chunk_number, failure_report_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)

    response = _client().post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "boom"


def test_retry_route_double_click_old_id_becomes_stale(monkeypatch):
    calls = {"count": 0}

    async def fake_retry(run_id, chunk_number, failure_report_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "status": "awaiting_chunk_approval",
                "run_id": run_id,
                "chunk_number": chunk_number,
                "failure_report_id": "new-frid",
            }
        return _retry_ineligible("stale_failure_report_id", 409)

    monkeypatch.setattr("backend.routes.chunks.retry_failed_chunk", fake_retry)
    client = _client()

    first = client.post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )
    second = client.post(
        "/runs/run-123/chunks/2/retry",
        json={"failure_report_id": "old-frid"},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "awaiting_chunk_approval"
    assert second.status_code == 409
    assert second.json()["status"] == "retry_ineligible"
    assert second.json()["reason"] == "stale_failure_report_id"
