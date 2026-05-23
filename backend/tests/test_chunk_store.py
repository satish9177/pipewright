"""
test_chunk_store.py
Tests for Phase 2B chunk plan persistence.
No API calls. No chunk execution.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
    get_previous_chunks_context,
    reject_chunk_plan,
    save_chunk_completion_summary,
    save_chunks_to_db,
    update_chunk_status,
)
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
        name=f"Chunk Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def make_triage(run_id: str, project_id: str, chunks: int = 2) -> TriageResult:
    definitions = []
    for number in range(1, chunks + 1):
        definitions.append(ChunkDefinition(
            chunk_number=number,
            title=f"Chunk {number}",
            description=f"Do chunk {number}",
            files_expected=[f"file_{number}.py"],
            depends_on=[] if number == 1 else [number - 1],
            risk_level="low",
            token_estimate=100 * number,
            requires_human_review=False,
            rationale="Ordered dependency",
        ))

    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Build chunk planning",
        complexity="medium" if chunks > 1 else "easy",
        total_chunks=chunks,
        chunks=definitions,
        reasoning="Split by dependency order.",
    )


def create_parent_run(run_id: str, project_id: str):
    init_db()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step)
            VALUES
            (:id, :project_id, 'Feature', 'running', 'chunk_plan')
        """), {"id": run_id, "project_id": project_id})


def test_create_chunked_run_creates_exactly_one_parent_run(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = make_triage(run_id, project["id"])

    create_chunked_run(run_id, project["id"], "Build chunk planning", triage)

    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()[0]

    assert count == 1


def test_save_chunks_to_db_stores_chunks_with_same_run_id(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_parent_run(run_id, project["id"])

    count = save_chunks_to_db(run_id, project["id"], make_triage(run_id, project["id"]))

    assert count == 2
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT run_id FROM chunks
            WHERE run_id = :run_id
        """), {"run_id": run_id}).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == run_id


def test_chunk_plan_status_starts_awaiting_approval(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)

    response = create_chunked_run(
        run_id,
        project["id"],
        "Build chunk planning",
        make_triage(run_id, project["id"]),
    )

    assert response.chunk_plan_status == "awaiting_approval"


def test_approve_chunk_plan_updates_status_and_does_not_execute(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Build chunk planning",
        make_triage(run_id, project["id"]),
    )

    response = approve_chunk_plan(run_id)

    assert response.chunk_plan_status == "approved"
    assert all(chunk.status == "pending" for chunk in response.chunks)


def test_reject_chunk_plan_updates_status(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Build chunk planning",
        make_triage(run_id, project["id"]),
    )

    response = reject_chunk_plan(run_id, "not ready")

    assert response.chunk_plan_status == "rejected"


def test_get_chunk_plan_status_returns_chunks_and_triage(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Build chunk planning",
        make_triage(run_id, project["id"]),
    )

    response = get_chunk_plan_status(run_id)

    assert response.triage is not None
    assert len(response.chunks) == 2


def test_previous_chunks_context_ignores_incomplete_chunks(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Build chunk planning",
        make_triage(run_id, project["id"]),
    )

    update_chunk_status(run_id, 1, "complete")
    save_chunk_completion_summary(run_id, 1, {"summary": "Chunk one done"})

    context = get_previous_chunks_context(run_id, 3)

    assert "Chunk 1" in context
    assert "Chunk 2" not in context


def test_previous_chunks_context_handles_null_completion_summary(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Build chunk planning",
        make_triage(run_id, project["id"]),
    )

    update_chunk_status(run_id, 1, "complete")

    context = get_previous_chunks_context(run_id, 2)

    assert "Summary: not available (crash recovery)" in context


def test_save_chunks_to_db_is_idempotent_for_duplicate_chunks(tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_parent_run(run_id, project["id"])
    triage = make_triage(run_id, project["id"])

    first = save_chunks_to_db(run_id, project["id"], triage)
    second = save_chunks_to_db(run_id, project["id"], triage)

    assert first == 2
    assert second == 2
