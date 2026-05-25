"""
Tests for stale approval gate timeout cleanup.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.runtime.approval_gate_recovery import timeout_stale_approval_gates

pytestmark = pytest.mark.unit


@pytest.fixture()
def gate_run(tmp_repo):
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO projects (id, name, repo_path, test_command, status)
            VALUES (:project_id, 'Gate Recovery', :repo_path, 'python --version', 'active')
        """), {"project_id": project_id, "repo_path": str(tmp_repo)})
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status)
            VALUES (:run_id, :project_id, 'Gate cleanup', 'paused')
        """), {"run_id": run_id, "project_id": project_id})
    yield run_id, project_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM approval_gates WHERE run_id = :run_id"), {"run_id": run_id})
        conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {"run_id": run_id})
        conn.execute(text("DELETE FROM projects WHERE id = :project_id"), {"project_id": project_id})


def _insert_gate(run_id: str, status: str, created_at: str) -> str:
    gate_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO approval_gates
            (id, run_id, step, status, approval_type, chunk_number, created_at)
            VALUES (:id, :run_id, 'pre-merge', :status, 'legacy', 0, :created_at)
        """), {
            "id": gate_id,
            "run_id": run_id,
            "status": status,
            "created_at": created_at,
        })
    return gate_id


def _gate_status(gate_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status FROM approval_gates WHERE id = :id
        """), {"id": gate_id}).scalar_one()


def test_old_pending_iso_gate_times_out(gate_run):
    run_id, _project_id = gate_run
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    gate_id = _insert_gate(run_id, "pending", old)

    timeout_stale_approval_gates(hours=2)

    assert _gate_status(gate_id) == "timeout"


def test_old_pending_sqlite_gate_times_out(gate_run):
    run_id, _project_id = gate_run
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    gate_id = _insert_gate(run_id, "pending", old)

    timeout_stale_approval_gates(hours=2)

    assert _gate_status(gate_id) == "timeout"


def test_recent_pending_gate_stays_pending(gate_run):
    run_id, _project_id = gate_run
    recent = datetime.now(timezone.utc).isoformat()
    gate_id = _insert_gate(run_id, "pending", recent)

    timeout_stale_approval_gates(hours=2)

    assert _gate_status(gate_id) == "pending"


def test_decided_gates_are_not_touched(gate_run):
    run_id, _project_id = gate_run
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    approved = _insert_gate(run_id, "approved", old)
    rejected = _insert_gate(run_id, "rejected", old)

    timeout_stale_approval_gates(hours=2)

    assert _gate_status(approved) == "approved"
    assert _gate_status(rejected) == "rejected"
