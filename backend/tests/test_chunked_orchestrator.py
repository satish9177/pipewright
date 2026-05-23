"""
test_chunked_orchestrator.py
Tests for Phase 2B-4A sequential chunk execution.
No real AI calls, no real GitHub, no push.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.models.handoff import (
    CoderHandoff,
    FileChange,
    PatchResult,
    PipelineTestResult,
    PlannerHandoff,
)
from backend.pipeline import chunked_orchestrator
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
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
        name=f"Execution Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def make_triage(run_id: str, project_id: str, chunks: int = 1) -> TriageResult:
    definitions = []
    for number in range(1, chunks + 1):
        definitions.append(ChunkDefinition(
            chunk_number=number,
            title=f"Chunk {number}",
            description=f"Do chunk {number}",
            files_expected=[f"file_{number}.py"],
            depends_on=[] if number == 1 else [number - 1],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="Sequential work",
        ))
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Execute chunks",
        complexity="medium" if chunks > 1 else "easy",
        total_chunks=chunks,
        chunks=definitions,
        reasoning="Chunked execution test",
    )


def create_run(tmp_repo, tracked_runs, chunks: int = 1, approved: bool = True):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Execute chunks",
        make_triage(run_id, project["id"], chunks),
    )
    if approved:
        approve_chunk_plan(run_id)
    return run_id, project


def make_planner_result(run_id: str) -> PlannerHandoff:
    return PlannerHandoff(
        run_id=run_id,
        feature_description="enriched",
        goal="Implement the chunk.",
        steps=["Plan step", "Code step"],
        files_to_create=[],
        files_to_modify=[],
        files_to_read=[],
        out_of_scope=[],
        risks=[],
        suggested_memory_entries=[],
    )


def make_coder_result(run_id: str, chunk_number: int = 1) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="enriched",
        files_changed=[
            FileChange(
                path=f"created_{chunk_number}.py",
                action="create",
                content="print('ok')\n",
                reason="create file",
            ),
            FileChange(
                path=f"modified_{chunk_number}.py",
                action="modify",
                content="print('changed')\n",
                reason="modify file",
            ),
            FileChange(
                path=f"deleted_{chunk_number}.py",
                action="delete",
                content=None,
                reason="delete file",
            ),
        ],
        summary=f"Chunk {chunk_number} coded",
        suggested_memory_entries=["remember chunk"],
    )


def make_patch_result(run_id: str) -> PatchResult:
    return PatchResult(
        run_id=run_id,
        success=True,
        diff="diff",
        pre_patch_git_hash="abc",
        post_patch_git_hash="def",
        files_applied=["created.py"],
    )


def make_test_result(run_id: str, passed: bool = True) -> PipelineTestResult:
    return PipelineTestResult(
        run_id=run_id,
        passed=passed,
        output="1 passed" if passed else "1 failed",
        total_tests=1,
        passed_tests=1 if passed else 0,
        failed_tests=0 if passed else 1,
    )


def patch_git_preflight(monkeypatch, calls=None):
    monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: None,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "create_or_checkout_branch",
        lambda branch, repo: calls.append(("branch", branch, repo)) if calls is not None else None,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        lambda files, message, repo: calls.append(("commit", files, message, repo)) if calls is not None else "hash",
    )


def patch_success_pipeline(monkeypatch, run_id: str, calls=None):
    async def fake_planner(feature_description, run_id, chunk_number=0):
        if calls is not None:
            calls.append(("planner", chunk_number, feature_description))
        return make_planner_result(run_id)

    async def fake_coder(plan, run_id, chunk_number=0):
        if calls is not None:
            calls.append(("coder", chunk_number))
        return make_coder_result(run_id, chunk_number)

    def fake_patch(code, run_id, chunk_number=0):
        if calls is not None:
            calls.append(("patch", chunk_number))
        return make_patch_result(run_id)

    def fake_tests(patch, run_id, chunk_number=0):
        if calls is not None:
            calls.append(("test", chunk_number))
        return make_test_result(run_id, True)

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)
    monkeypatch.setattr(chunked_orchestrator, "apply_patch", fake_patch)
    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)


@pytest.mark.asyncio
async def test_execute_refuses_when_chunk_plan_not_approved(tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, approved=False)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.execute_approved_chunks(run_id)


@pytest.mark.asyncio
async def test_double_execution_guard_returns_already_running(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    update_chunk_status(run_id, 1, "running")
    called = {"planner": False}

    async def fake_planner(*args, **kwargs):
        called["planner"] = True

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "already_running"
    assert called["planner"] is False


@pytest.mark.asyncio
async def test_dirty_target_repo_blocks_fresh_execution(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    called = {"planner": False}
    monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: (_ for _ in ()).throw(RuntimeError("[GIT] dirty")),
    )

    async def fake_planner(*args, **kwargs):
        called["planner"] = True

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.execute_approved_chunks(run_id)

    assert called["planner"] is False
    with engine.connect() as conn:
        status = conn.execute(text("""
            SELECT status FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()[0]
    assert status == "failed"


@pytest.mark.asyncio
async def test_branch_is_created_or_checked_out(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    assert ("branch", f"pipewright/{run_id[:8]}", project["repo_path"]) in calls


@pytest.mark.asyncio
async def test_chunks_execute_sequentially(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    ordered = [call for call in calls if call[0] in {"planner", "coder", "patch", "test"}]
    assert ordered == [
        ("planner", 1, ordered[0][2]),
        ("coder", 1),
        ("patch", 1),
        ("test", 1),
        ("planner", 2, ordered[4][2]),
        ("coder", 2),
        ("patch", 2),
        ("test", 2),
    ]


@pytest.mark.asyncio
async def test_previous_chunks_context_is_injected(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(
        chunked_orchestrator,
        "get_previous_chunks_context",
        lambda run_id, chunk_number: "PREVIOUS_CONTEXT",
    )

    await chunked_orchestrator.execute_approved_chunks(run_id)

    planner_call = next(call for call in calls if call[0] == "planner")
    assert "PREVIOUS_CONTEXT" in planner_call[2]


@pytest.mark.asyncio
async def test_file_index_relevant_paths_are_injected(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(
        chunked_orchestrator,
        "get_relevant_files",
        lambda project_id, query, limit=20: [{
            "path": "backend/app/routers/workflows.py",
            "file_type": "route",
            "token_estimate": 321,
        }],
    )

    await chunked_orchestrator.execute_approved_chunks(run_id)

    planner_call = next(call for call in calls if call[0] == "planner")
    assert "backend/app/routers/workflows.py" in planner_call[2]


@pytest.mark.asyncio
async def test_chunk_number_propagation(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    assert ("planner", 1, calls[1][2]) in calls
    assert ("coder", 1) in calls
    assert ("patch", 1) in calls
    assert ("test", 1) in calls
    assert ("coder", 2) in calls
    assert ("patch", 2) in calls
    assert ("test", 2) in calls


@pytest.mark.asyncio
async def test_successful_chunk_commits_exactly_touched_files(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    commit_call = next(call for call in calls if call[0] == "commit")
    assert commit_call[1] == ["created_1.py", "modified_1.py", "deleted_1.py"]


@pytest.mark.asyncio
async def test_git_commit_failure_marks_failed_and_stops(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def fail_commit(files, message, repo):
        raise RuntimeError("[GIT] commit failed")

    monkeypatch.setattr(chunked_orchestrator.local_git, "commit_files", fail_commit)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert ("planner", 2, "unused") not in calls
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "failed"


@pytest.mark.asyncio
async def test_test_failure_marks_failed_stops_and_does_not_commit(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failed_tests(patch, run_id, chunk_number=0):
        calls.append(("test", chunk_number))
        return make_test_result(run_id, False)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert not any(call[0] == "commit" for call in calls)
    assert not any(call[0] == "coder" and call[1] == 2 for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "failed"


@pytest.mark.asyncio
async def test_completion_summary_stored(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    summaries = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(
        chunked_orchestrator,
        "save_chunk_completion_summary",
        lambda run_id, chunk_number, summary: summaries.append(summary),
    )

    await chunked_orchestrator.execute_approved_chunks(run_id)

    assert summaries
    summary = summaries[0]
    assert summary["files_created"] == ["created_1.py"]
    assert summary["files_modified"] == ["modified_1.py"]
    assert summary["files_deleted"] == ["deleted_1.py"]
    assert "Coder summary" in summary["summary"]


@pytest.mark.asyncio
async def test_all_chunks_complete_marks_run_chunks_completed(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "chunks_completed"
    assert result["completed_chunks"] == 2
    status = get_chunk_plan_status(run_id)
    assert all(chunk.status == "completed" for chunk in status.chunks)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, current_chunk_number
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "chunks_completed"
    assert row[1] == 2


def test_scope_guard_no_resume_or_push_or_pr():
    assert not hasattr(chunked_orchestrator, "resume_chunked_pipeline")
    paths = {route.path for route in app.routes}
    assert "/runs/{run_id}/chunks/resume" not in paths
    assert not hasattr(chunked_orchestrator, "create_pull_request")
    assert not hasattr(chunked_orchestrator.local_git, "push_was_called")


def test_execute_route_calls_execute_approved_chunks(monkeypatch):
    called = {"run_id": None}

    async def fake_execute(run_id):
        called["run_id"] = run_id
        return {"status": "chunks_completed", "run_id": run_id}

    monkeypatch.setattr("backend.routes.chunks.execute_approved_chunks", fake_execute)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/execute")

    assert response.status_code == 200
    assert response.json()["status"] == "chunks_completed"
    assert called["run_id"] == "run-123"
