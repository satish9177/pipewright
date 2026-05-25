"""
Tests for server-start recovery of interrupted run state.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.main import app
from backend.db.database import engine
from backend.runtime.startup_recovery import (
    RESTART_RECOVERY_MESSAGE,
    recover_interrupted_runs,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def recovery_rows(tmp_repo):
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO projects (id, name, repo_path, test_command, status)
            VALUES (:project_id, 'Recovery Project', :repo_path, 'python --version', 'active')
        """), {"project_id": project_id, "repo_path": str(tmp_repo)})
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step)
            VALUES (:run_id, :project_id, 'Recover', 'running_chunks', 'chunk_1')
        """), {"run_id": run_id, "project_id": project_id})
        conn.execute(text("""
            INSERT INTO chunks
            (id, run_id, project_id, chunk_number, title, description, status, started_at)
            VALUES (:chunk_id, :run_id, :project_id, 1, 'Chunk', 'Running', 'running', :started_at)
        """), {
            "chunk_id": chunk_id,
            "run_id": run_id,
            "project_id": project_id,
            "started_at": "2026-05-25T00:00:00+00:00",
        })
    yield run_id, chunk_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {"run_id": run_id})
        conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {"run_id": run_id})
        conn.execute(text("DELETE FROM projects WHERE id = :project_id"), {"project_id": project_id})


def test_recovery_resets_running_chunk_and_interrupts_run(recovery_rows):
    run_id, _chunk_id = recovery_rows

    result = recover_interrupted_runs()

    assert result["chunks_reset"] >= 1
    assert result["runs_interrupted"] >= 1
    with engine.connect() as conn:
        chunk = conn.execute(text("""
            SELECT status, started_at, error_message FROM chunks WHERE run_id = :run_id
        """), {"run_id": run_id}).fetchone()
        run = conn.execute(text("""
            SELECT status, current_step FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert chunk[0] == "pending"
    assert chunk[1] is None
    assert chunk[2] == RESTART_RECOVERY_MESSAGE
    assert run[0] == "interrupted"
    assert run[1] == "interrupted"


def test_recovery_does_not_touch_completed_failed_or_approved_chunks(tmp_repo):
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    statuses = ["completed", "failed", "approved", "rejected"]
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO projects (id, name, repo_path, test_command, status)
            VALUES (:project_id, 'Recovery Safe', :repo_path, 'python --version', 'active')
        """), {"project_id": project_id, "repo_path": str(tmp_repo)})
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step)
            VALUES (:run_id, :project_id, 'Recover', 'failed', 'done')
        """), {"run_id": run_id, "project_id": project_id})
        for index, status in enumerate(statuses, start=1):
            conn.execute(text("""
                INSERT INTO chunks
                (id, run_id, project_id, chunk_number, title, description, status)
                VALUES (:id, :run_id, :project_id, :chunk_number, 'Chunk', 'Done', :status)
            """), {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "project_id": project_id,
                "chunk_number": index,
                "status": status,
            })

    recover_interrupted_runs()

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT status FROM chunks WHERE run_id = :run_id ORDER BY chunk_number
        """), {"run_id": run_id}).fetchall()
    assert [row[0] for row in rows] == statuses

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {"run_id": run_id})
        conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {"run_id": run_id})
        conn.execute(text("DELETE FROM projects WHERE id = :project_id"), {"project_id": project_id})


def test_fastapi_lifespan_runs_startup_recovery(monkeypatch):
    calls = {"approval_cleanup": 0, "run_recovery": 0}

    def fake_approval_cleanup():
        calls["approval_cleanup"] += 1
        return 0

    def fake_run_recovery():
        calls["run_recovery"] += 1
        return {"chunks_reset": 0, "runs_interrupted": 0}

    monkeypatch.setattr("backend.main.timeout_stale_approval_gates", fake_approval_cleanup)
    monkeypatch.setattr("backend.main.recover_interrupted_runs", fake_run_recovery)

    with TestClient(app):
        pass

    assert calls == {"approval_cleanup": 1, "run_recovery": 1}
