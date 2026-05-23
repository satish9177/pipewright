"""
test_chunk_routes.py
Tests for Phase 2B chunk planning routes.
No API calls. Triage is mocked.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.chunk_store import create_chunked_run
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {
                "run_id": run_id,
            })


def make_project(tmp_repo):
    return create_project(
        name=f"Route Chunk Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def make_triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Add route chunks",
        complexity="easy",
        total_chunks=1,
        reasoning="One chunk is enough.",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Route chunk",
            description="Plan route chunk.",
            files_expected=["backend/routes/chunks.py"],
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="Small route change.",
        )],
    )


def test_post_runs_chunked_returns_awaiting_approval_plan(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "Add route chunks",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["total_chunks"] == 1
    assert len(data["chunks"]) == 1


def test_triage_failure_does_not_create_parent_run(monkeypatch, tmp_repo):
    project = make_project(tmp_repo)
    feature = f"Failure feature {uuid.uuid4()}"

    async def failing_triage(run_id, project_id, feature_description):
        raise RuntimeError("triage failed")

    monkeypatch.setattr("backend.routes.chunks.run_triage", failing_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": feature,
    })

    assert response.status_code == 500
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs
            WHERE feature_description = :feature
        """), {"feature": feature}).fetchone()[0]
    assert count == 0


def test_missing_project_returns_404_before_triage(monkeypatch):
    called = {"value": False}

    async def fake_triage(run_id, project_id, feature_description):
        called["value"] = True
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": "proj-missing",
        "feature_description": "Add route chunks",
    })

    assert response.status_code == 404
    assert called["value"] is False


def test_get_chunks_route_returns_plan(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/chunks")

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id


def test_approve_endpoint_approves_only_and_does_not_execute(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/chunks/approve")

    assert response.status_code == 200
    data = response.json()
    assert data["chunk_plan_status"] == "approved"
    assert data["chunks"][0]["status"] == "pending"


def test_reject_endpoint_rejects_only(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add route chunks",
        make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/chunks/reject", json={
        "reason": "not ready",
    })

    assert response.status_code == 200
    assert response.json()["chunk_plan_status"] == "rejected"


def test_execute_route_exists_and_resume_route_does_not_exist():
    paths = {route.path for route in app.routes}

    assert "/runs/{run_id}/chunks/execute" in paths
    assert "/runs/{run_id}/chunks/resume" not in paths
