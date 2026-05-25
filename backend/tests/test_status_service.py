"""
test_status_service.py
Tests for centralized run/chunk status update helpers.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.core import status_service
from backend.core.status_service import update_chunk_status, update_run_status
from backend.core.statuses import ChunkStatusValue, RunStatus
from backend.db.database import engine, init_db
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


def _create_project(tmp_repo) -> dict:
    return create_project(
        name=f"Status Service Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _create_run(project_id: str, tracked_runs: list[str]) -> str:
    init_db()
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step)
            VALUES
            (:id, :project_id, 'Feature', 'running', 'start')
        """), {
            "id": run_id,
            "project_id": project_id,
        })
    return run_id


def _create_chunk(run_id: str, project_id: str, chunk_number: int = 1) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO chunks
            (
                id, run_id, project_id, chunk_number, title, description,
                status
            )
            VALUES
            (
                :id, :run_id, :project_id, :chunk_number, 'Chunk 1',
                'Do the work', 'pending'
            )
        """), {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "project_id": project_id,
            "chunk_number": chunk_number,
        })


def test_update_run_status_writes_status_current_step_and_chunk_number(
    tmp_repo,
    tracked_runs,
):
    project = _create_project(tmp_repo)
    run_id = _create_run(project["id"], tracked_runs)

    update_run_status(
        run_id,
        RunStatus.RUNNING_CHUNKS,
        "chunk_2",
        current_chunk_number=2,
    )

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, current_step, current_chunk_number
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()

    assert row[0] == "running_chunks"
    assert row[1] == "chunk_2"
    assert row[2] == 2


def test_update_chunk_status_writes_status_and_error_message(tmp_repo, tracked_runs):
    project = _create_project(tmp_repo)
    run_id = _create_run(project["id"], tracked_runs)
    _create_chunk(run_id, project["id"])

    update_chunk_status(
        run_id,
        1,
        ChunkStatusValue.FAILED,
        "Tests failed",
    )

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, error_message
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()

    assert row[0] == "failed"
    assert row[1] == "Tests failed"


def test_update_run_status_publishes_existing_event_shape(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    events = []
    monkeypatch.setattr(status_service.event_bus, "publish", events.append)
    project = _create_project(tmp_repo)
    run_id = _create_run(project["id"], tracked_runs)

    update_run_status(
        run_id,
        RunStatus.FAILED,
        "chunk_1_failed",
        current_chunk_number=1,
        publish_event=True,
    )

    assert len(events) == 1
    event = events[0]
    assert event.kind == "run_status_changed"
    assert event.stage == "orchestrator"
    assert event.message == "Run -> failed"
    assert event.data == {
        "to_status": "failed",
        "current_chunk_number": 1,
    }


def test_update_chunk_status_event_failure_is_best_effort(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    def fail_publish(_event):
        raise RuntimeError("event bus unavailable")

    monkeypatch.setattr(status_service.event_bus, "publish", fail_publish)
    project = _create_project(tmp_repo)
    run_id = _create_run(project["id"], tracked_runs)
    _create_chunk(run_id, project["id"])

    update_chunk_status(
        run_id,
        1,
        ChunkStatusValue.COMPLETED,
        publish_event=True,
    )

    with engine.connect() as conn:
        status = conn.execute(text("""
            SELECT status
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()[0]

    assert status == "completed"
