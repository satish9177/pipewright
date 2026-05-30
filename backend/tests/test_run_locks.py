"""
test_run_locks.py
Tests for in-process project/repo execution locks.
No API calls. No Gemini.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.main import app
from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.chunk_store import approve_chunk_plan, create_chunked_run
from backend.pipeline import chunked_orchestrator, pr_orchestrator
from backend.pipeline.run_locks import (
    PROJECT_LOCK_CONFLICT_MESSAGE,
    ProjectRepoLockError,
    is_project_locked,
    project_repo_lock,
    project_repo_lock_sync,
)
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


def _triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Lock test",
        complexity="easy",
        total_chunks=1,
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="Chunk 1",
                description="Do chunk 1",
                files_expected=["file_1.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="Lock test chunk",
            )
        ],
        reasoning="Lock test",
    )


def _project(tmp_repo):
    return create_project(
        name=f"Lock Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _chunked_run(tmp_repo, tracked_runs):
    project = _project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Lock test",
        _triage(run_id, project["id"]),
    )
    approve_chunk_plan(run_id)
    return run_id, project


def _insert_push_run(run_id: str, project_id: str, status: str = "final_approved"):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step, created_at)
            VALUES
            (:id, :project_id, 'Lock test', :status, 'final_approval', CURRENT_TIMESTAMP)
        """), {
            "id": run_id,
            "project_id": project_id,
            "status": status,
        })


async def test_async_lock_releases_after_success():
    project_id = f"proj-{uuid.uuid4().hex[:8]}"

    async with project_repo_lock(project_id):
        assert is_project_locked(project_id)

    assert not is_project_locked(project_id)


async def test_async_lock_releases_after_exception():
    project_id = f"proj-{uuid.uuid4().hex[:8]}"

    with pytest.raises(RuntimeError):
        async with project_repo_lock(project_id):
            raise RuntimeError("boom")

    assert not is_project_locked(project_id)


async def test_same_project_concurrent_async_locks_cannot_both_proceed():
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    entered = []

    async def hold_lock():
        async with project_repo_lock(project_id):
            entered.append("first")
            with pytest.raises(ProjectRepoLockError):
                async with project_repo_lock(project_id):
                    entered.append("second")

    await hold_lock()

    assert entered == ["first"]


async def test_different_projects_can_lock_concurrently():
    first_project = f"proj-{uuid.uuid4().hex[:8]}"
    second_project = f"proj-{uuid.uuid4().hex[:8]}"

    async with project_repo_lock(first_project):
        async with project_repo_lock(second_project):
            assert is_project_locked(first_project)
            assert is_project_locked(second_project)

    assert not is_project_locked(first_project)
    assert not is_project_locked(second_project)


def test_sync_lock_blocks_same_project_and_allows_different_projects():
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    other_project_id = f"proj-{uuid.uuid4().hex[:8]}"

    with project_repo_lock_sync(project_id):
        with pytest.raises(ProjectRepoLockError) as error:
            with project_repo_lock_sync(project_id):
                pass
        assert str(error.value) == PROJECT_LOCK_CONFLICT_MESSAGE

        with project_repo_lock_sync(other_project_id):
            assert is_project_locked(other_project_id)

    assert not is_project_locked(project_id)
    assert not is_project_locked(other_project_id)


def test_legacy_run_route_is_disabled_even_when_project_locked(tmp_repo):
    # The legacy /run route is retired (410) and no longer participates in
    # repo locking; it short-circuits before any lock is reserved.
    project = _project(tmp_repo)
    client = TestClient(app)

    with project_repo_lock_sync(project["id"]):
        response = client.post("/run", json={
            "project_id": project["id"],
            "feature_description": "Add locked run feature",
        })

    assert response.status_code == 410
    assert response.json()["detail"] == (
        "Legacy run endpoint is disabled. Use /runs/chunked."
    )


async def test_execute_refuses_when_same_project_locked(tmp_repo, tracked_runs):
    run_id, project = _chunked_run(tmp_repo, tracked_runs)

    with project_repo_lock_sync(project["id"]):
        with pytest.raises(ProjectRepoLockError):
            await chunked_orchestrator.execute_approved_chunks(run_id)


async def test_resume_refuses_when_same_project_locked(tmp_repo, tracked_runs):
    run_id, project = _chunked_run(tmp_repo, tracked_runs)

    with project_repo_lock_sync(project["id"]):
        with pytest.raises(ProjectRepoLockError):
            await chunked_orchestrator.resume_chunked_pipeline(run_id)


def test_approve_chunk_refuses_when_same_project_locked(tmp_repo, tracked_runs):
    run_id, project = _chunked_run(tmp_repo, tracked_runs)

    with project_repo_lock_sync(project["id"]):
        with pytest.raises(ProjectRepoLockError):
            chunked_orchestrator.approve_chunk_and_commit(run_id, 1)


def test_reject_chunk_refuses_when_same_project_locked(tmp_repo, tracked_runs):
    run_id, project = _chunked_run(tmp_repo, tracked_runs)

    with project_repo_lock_sync(project["id"]):
        with pytest.raises(ProjectRepoLockError):
            chunked_orchestrator.reject_chunk_and_rollback(run_id, 1)


def test_push_pr_refuses_when_same_project_locked(tmp_repo, tracked_runs):
    project = _project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    _insert_push_run(run_id, project["id"])

    with project_repo_lock_sync(project["id"]):
        with pytest.raises(ProjectRepoLockError):
            pr_orchestrator.push_and_create_pr(run_id)
