"""
test_statuses.py
Tests for centralized status constants.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.core.statuses import (
    ApprovalStatus,
    CheckpointStatus,
    ChunkPlanStatus,
    ChunkStatusValue,
    GateStatus,
    ProjectStatus,
    RunStatus,
)
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
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {
                "run_id": run_id,
            })


def _make_triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Add status constants",
        complexity="easy",
        total_chunks=1,
        reasoning="One chunk is enough.",
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="Status constants",
                description="Add status constants.",
                files_expected=["backend/core/statuses.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="Small foundation change.",
            )
        ],
    )


def test_status_constants_match_existing_string_values():
    assert RunStatus.RUNNING == "running"
    assert RunStatus.RUNNING_CHUNKS == "running_chunks"
    assert RunStatus.PAUSED == "paused"
    assert RunStatus.FAILED == "failed"
    assert RunStatus.REJECTED == "rejected"
    assert RunStatus.COMPLETE == "complete"
    assert RunStatus.STARTED == "started"
    assert RunStatus.AWAITING_CHUNK_PLAN_APPROVAL == "awaiting_chunk_plan_approval"
    assert RunStatus.CHUNK_PLAN_APPROVED == "chunk_plan_approved"
    assert RunStatus.AWAITING_CHUNK_APPROVAL == "awaiting_chunk_approval"
    assert RunStatus.CHUNK_APPROVED == "chunk_approved"
    assert RunStatus.AWAITING_FINAL_APPROVAL == "awaiting_final_approval"
    assert RunStatus.FINAL_APPROVED == "final_approved"
    assert RunStatus.FINAL_REJECTED == "final_rejected"
    assert RunStatus.PUSHING == "pushing"
    assert RunStatus.PUSH_FAILED == "push_failed"

    assert ChunkStatusValue.PENDING == "pending"
    assert ChunkStatusValue.RUNNING == "running"
    assert ChunkStatusValue.COMPLETED == "completed"
    assert ChunkStatusValue.FAILED == "failed"
    assert ChunkStatusValue.REJECTED == "rejected"
    assert ChunkStatusValue.AWAITING_CHUNK_APPROVAL == "awaiting_chunk_approval"

    assert ChunkPlanStatus.AWAITING_APPROVAL == "awaiting_approval"
    assert ChunkPlanStatus.APPROVED == "approved"
    assert ChunkPlanStatus.REJECTED == "rejected"
    assert ChunkPlanStatus.NONE == "none"

    assert ApprovalStatus.PENDING == "pending"
    assert ApprovalStatus.APPROVED == "approved"
    assert ApprovalStatus.REJECTED == "rejected"
    assert ApprovalStatus.TIMEOUT == "timeout"

    assert GateStatus.PENDING == "pending"
    assert GateStatus.APPROVED == "approved"
    assert GateStatus.REJECTED == "rejected"
    assert GateStatus.TIMEOUT == "timeout"

    assert ProjectStatus.ACTIVE == "active"
    assert CheckpointStatus.COMPLETE == "complete"


def test_status_constants_are_plain_strings_not_enums():
    assert isinstance(RunStatus.RUNNING, str)
    assert isinstance(ChunkStatusValue.PENDING, str)
    assert isinstance(ChunkPlanStatus.APPROVED, str)
    assert isinstance(ApprovalStatus.REJECTED, str)


def test_chunk_plan_route_status_strings_are_unchanged(tmp_repo, tracked_runs):
    project = create_project(
        name=f"Status Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Add status constants",
        _make_triage(run_id, project["id"]),
    )
    client = TestClient(app)

    response = client.post(f"/runs/{run_id}/chunks/approve")

    assert response.status_code == 200
    data = response.json()
    assert data["chunk_plan_status"] == "approved"
    assert data["chunks"][0]["status"] == "pending"

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, chunk_plan_status
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "chunk_plan_approved"
    assert row[1] == "approved"
