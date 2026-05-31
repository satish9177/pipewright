"""
test_chunked_orchestrator.py
Tests for Phase 2B-4A sequential chunk execution.
No real AI calls, no real GitHub, no push.
"""

import json
import uuid
import inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.events.event_bus import clear_all_events_for_tests, get_buffered_events
from backend.main import app
from backend.checkpoint.checkpoint_store import save_checkpoint
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
    save_chunk_completion_summary,
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
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
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


def make_triage(
    run_id: str,
    project_id: str,
    chunks: int = 1,
    review_chunks: set[int] | None = None,
) -> TriageResult:
    review_chunks = review_chunks or set()
    definitions = []
    for number in range(1, chunks + 1):
        needs_review = number in review_chunks
        definitions.append(ChunkDefinition(
            chunk_number=number,
            title=f"Chunk {number}",
            description=f"Do chunk {number}",
            files_expected=[
                f"created_{number}.py",
                f"modified_{number}.py",
                f"deleted_{number}.py",
            ],
            depends_on=[] if number == 1 else [number - 1],
            risk_level="high" if needs_review else "low",
            token_estimate=100,
            requires_human_review=needs_review,
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


def create_run(
    tmp_repo,
    tracked_runs,
    chunks: int = 1,
    approved: bool = True,
    review_chunks: set[int] | None = None,
):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Execute chunks",
        make_triage(run_id, project["id"], chunks, review_chunks),
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


def make_empty_coder_result(run_id: str) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="enriched",
        files_changed=[],
        summary="No files changed",
        suggested_memory_entries=[],
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
        "assert_not_on_stale_pipewright_branch",
        lambda repo, run_id: None,
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
    # After a real patch the working tree is dirty (effective changes present).
    # Make this deterministic so the no-effective-change commit guard does not
    # trip on the fake pipeline. Tests run under .pytest_tmp, which is gitignored,
    # so the ambient working tree would otherwise look clean in a fresh checkout.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "is_working_tree_clean",
        lambda repo_path: False,
    )


def patch_success_pipeline(monkeypatch, run_id: str, calls=None):
    async def fake_planner(feature_description, run_id, chunk_number=0, **kwargs):
        if calls is not None:
            calls.append(("planner", chunk_number, feature_description))
        return make_planner_result(run_id)

    async def fake_coder(plan, run_id, chunk_number=0, **kwargs):
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
    # A successful patch produces effective on-disk changes, so the working
    # tree is dirty at commit time. Make this deterministic so the
    # no-effective-change commit guard does not trip on the fake pipeline.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "is_working_tree_clean",
        lambda repo_path: False,
    )


def patch_resume_git(monkeypatch, calls=None, branch_exists=True, clean=True):
    monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "branch_exists",
        lambda branch, repo: branch_exists,
    )

    def fake_run_git(args, repo):
        if calls is not None:
            calls.append(("run_git", args, repo))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(chunked_orchestrator.local_git, "run_git", fake_run_git)
    if clean:
        monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_clean_worktree", lambda repo: None)
        monkeypatch.setattr(chunked_orchestrator.local_git, "get_dirty_files", lambda repo: [])
    else:
        monkeypatch.setattr(
            chunked_orchestrator.local_git,
            "ensure_clean_worktree",
            lambda repo: (_ for _ in ()).throw(RuntimeError("[GIT] dirty")),
        )
        monkeypatch.setattr(chunked_orchestrator.local_git, "get_dirty_files", lambda repo: ["dirty.py"])

    monkeypatch.setattr(chunked_orchestrator.local_git, "commit_message_exists", lambda repo, prefix: True)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        lambda files, message, repo: calls.append(("commit", files, message, repo)) if calls is not None else "hash",
    )


def add_test_checkpoint(run_id: str, chunk_number: int):
    save_checkpoint(
        run_id=run_id,
        step="test",
        output={"passed": True},
        handoff_contract={"passed": True},
        git_hash="hash",
        tests_passed=True,
        chunk_number=chunk_number,
    )
    save_chunk_completion_summary(
        run_id,
        chunk_number,
        {"summary": f"chunk {chunk_number} done"},
    )


def add_code_checkpoint(run_id: str, chunk_number: int):
    coder = make_coder_result(run_id, chunk_number)
    save_checkpoint(
        run_id=run_id,
        step="code",
        output=coder.model_dump(),
        handoff_contract=coder.model_dump(),
        git_hash="pre-patch",
        tests_passed=False,
        step_completed=True,
        chunk_number=chunk_number,
    )
    return coder


def event_statuses(run_id: str, kind: str) -> list[str]:
    return [
        event.data["to_status"]
        for event in get_buffered_events(run_id)
        if event.kind == kind
    ]


def count_memory_facts(project_id: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT COUNT(*) FROM memory_facts WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchone()[0]


@pytest.mark.asyncio
async def test_execute_refuses_when_chunk_plan_not_approved(tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, approved=False)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.execute_approved_chunks(run_id)


@pytest.mark.asyncio
async def test_empty_coder_output_fails_before_patch_test_or_commit(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)

    async def fake_planner(*args, **kwargs):
        return make_planner_result(run_id)

    async def fake_coder(*args, **kwargs):
        return make_empty_coder_result(run_id)

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)
    monkeypatch.setattr(
        chunked_orchestrator,
        "apply_patch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("apply_patch should not be called")
        ),
    )
    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("run_tests should not be called")
        ),
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["error"] == chunked_orchestrator.NO_CHANGES_MESSAGE
    assert not any(call[0] == "commit" for call in calls)
    with engine.connect() as conn:
        chunk = conn.execute(text("""
            SELECT status, error_message
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
        run = conn.execute(text("""
            SELECT status, current_step
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert chunk[0] == "failed"
    assert chunk[1] == chunked_orchestrator.NO_CHANGES_MESSAGE
    assert run[0] == "failed"
    assert run[1] == "chunk_1_failed"


def test_commit_and_complete_chunk_refuses_empty_touched_files(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    plan_status = get_chunk_plan_status(run_id)
    chunk = plan_status.triage.chunks[0]
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("commit_files should not be called")
        ),
    )

    with pytest.raises(RuntimeError, match=chunked_orchestrator.NO_CHANGES_MESSAGE):
        chunked_orchestrator._commit_and_complete_chunk(
            run_id,
            chunk,
            make_empty_coder_result(run_id),
            str(tmp_repo),
            make_planner_result(run_id),
        )

    with engine.connect() as conn:
        chunk_row = conn.execute(text("""
            SELECT status, error_message
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
    assert chunk_row[0] == "failed"
    assert chunk_row[1] == chunked_orchestrator.NO_CHANGES_MESSAGE


def test_commit_and_complete_chunk_skips_commit_when_working_tree_clean(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    plan_status = get_chunk_plan_status(run_id)
    chunk = plan_status.triage.chunks[0]

    # Coder declared changes (non-empty touched_files) but the patch produced
    # no effective on-disk change, so git reports a clean working tree.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "is_working_tree_clean",
        lambda repo_path: True,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("commit_files should not be called")
        ),
    )

    with pytest.raises(RuntimeError, match="Patch produced no effective changes"):
        chunked_orchestrator._commit_and_complete_chunk(
            run_id,
            chunk,
            make_coder_result(run_id),
            str(tmp_repo),
            make_planner_result(run_id),
        )

    with engine.connect() as conn:
        chunk_row = conn.execute(text("""
            SELECT status, error_message
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
        run_row = conn.execute(text("""
            SELECT status, current_step
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()

    assert chunk_row[0] == "failed"
    assert chunk_row[1] == chunked_orchestrator.NO_EFFECTIVE_CHANGES_MESSAGE
    assert run_row[0] == "failed"
    assert run_row[1] == "chunk_1_failed"


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
async def test_fresh_execute_rejects_stale_pipewright_branch_before_checkout(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: None,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "assert_not_on_stale_pipewright_branch",
        lambda repo, run_id: (_ for _ in ()).throw(
            RuntimeError("[GIT] stale Pipewright branch")
        ),
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "create_or_checkout_branch",
        lambda branch, repo: calls.append(("branch", branch, repo)),
    )

    async def fake_planner(*args, **kwargs):
        calls.append(("planner",))

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)

    with pytest.raises(RuntimeError, match="stale Pipewright branch"):
        await chunked_orchestrator.execute_approved_chunks(run_id)

    assert not any(call[0] == "branch" for call in calls)
    assert not any(call[0] == "planner" for call in calls)


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
async def test_chunked_execution_emits_chunk_running_and_completed_events(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    clear_all_events_for_tests()
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    statuses = event_statuses(run_id, "chunk_status_changed")
    assert "running" in statuses
    assert "completed" in statuses
    events = [
        event for event in get_buffered_events(run_id)
        if event.kind == "chunk_status_changed"
    ]
    assert all(event.stage == "orchestrator" for event in events)


@pytest.mark.asyncio
async def test_final_approval_path_emits_run_status_event(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    clear_all_events_for_tests()
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    statuses = event_statuses(run_id, "run_status_changed")
    assert "awaiting_final_approval" in statuses


@pytest.mark.asyncio
async def test_high_risk_chunk_pauses_after_tests_before_commit(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(
        tmp_repo,
        tracked_runs,
        review_chunks={1},
    )
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_chunk_approval"
    assert result["chunk_number"] == 1
    assert result["approval_required"] is True
    assert any(call[0] == "planner" for call in calls)
    assert any(call[0] == "coder" for call in calls)
    assert any(call[0] == "patch" for call in calls)
    assert any(call[0] == "test" for call in calls)
    assert not any(call[0] == "commit" for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "awaiting_chunk_approval"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT pr.status, ag.approval_type, ag.chunk_number, ag.status
            FROM pipeline_runs pr
            JOIN approval_gates ag ON ag.run_id = pr.id
            WHERE pr.id = :run_id
              AND ag.approval_type = 'chunk'
        """), {"run_id": run_id}).fetchone()
        final_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
        """), {"run_id": run_id}).fetchone()[0]
    assert row[0] == "awaiting_chunk_approval"
    assert row[1] == "chunk"
    assert row[2] == 1
    assert row[3] == "pending"
    assert final_count == 0


@pytest.mark.asyncio
async def test_high_risk_chunk_pause_emits_run_and_chunk_status_events(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    clear_all_events_for_tests()
    run_id, _project = create_run(
        tmp_repo,
        tracked_runs,
        review_chunks={1},
    )
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_chunk_approval"
    assert "awaiting_chunk_approval" in event_statuses(
        run_id,
        "chunk_status_changed",
    )
    assert "awaiting_chunk_approval" in event_statuses(
        run_id,
        "run_status_changed",
    )


@pytest.mark.asyncio
async def test_execute_honors_persisted_human_review_flag_before_commit(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(
        tmp_repo,
        tracked_runs,
        chunks=2,
        review_chunks=set(),
    )
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE chunks
            SET requires_human_review = 1
            WHERE run_id = :run_id
              AND chunk_number = 2
        """), {"run_id": run_id})
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_chunk_approval"
    assert result["chunk_number"] == 2
    commit_calls = [call for call in calls if call[0] == "commit"]
    assert len(commit_calls) == 1
    assert commit_calls[0][1] == ["created_1.py", "modified_1.py", "deleted_1.py"]
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"
    assert status.chunks[1].status == "awaiting_chunk_approval"
    assert status.chunks[1].requires_human_review is True
    assert status.triage is not None
    assert status.triage.chunks[1].requires_human_review is False
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT pr.status, ag.approval_type, ag.chunk_number, ag.status
            FROM pipeline_runs pr
            JOIN approval_gates ag ON ag.run_id = pr.id
            WHERE pr.id = :run_id
              AND ag.approval_type = 'chunk'
        """), {"run_id": run_id}).fetchone()
        final_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
        """), {"run_id": run_id}).fetchone()[0]
    assert row[0] == "awaiting_chunk_approval"
    assert row[1] == "chunk"
    assert row[2] == 2
    assert row[3] == "pending"
    assert final_count == 0


@pytest.mark.asyncio
async def test_chunk_approve_works_after_persisted_flag_pause(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(
        tmp_repo,
        tracked_runs,
        chunks=2,
        review_chunks=set(),
    )
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE chunks
            SET requires_human_review = 1
            WHERE run_id = :run_id
              AND chunk_number = 2
        """), {"run_id": run_id})
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)
    assert result["status"] == "awaiting_chunk_approval"
    add_code_checkpoint(run_id, 2)

    approve_result = chunked_orchestrator.approve_chunk_and_commit(run_id, 2)

    assert approve_result["status"] == "chunk_approved"
    assert approve_result["chunk_number"] == 2
    commit_calls = [call for call in calls if call[0] == "commit"]
    assert len(commit_calls) == 2
    assert commit_calls[1][1] == ["created_2.py", "modified_2.py", "deleted_2.py"]
    status = get_chunk_plan_status(run_id)
    assert status.chunks[1].status == "completed"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'chunk'
              AND chunk_number = 2
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "approved"


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
async def test_no_final_approval_gate_created_if_chunk_fails(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failed_tests(patch, run_id, chunk_number=0):
        return make_test_result(run_id, False)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_failed_chunk_emits_failure_status_events(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    clear_all_events_for_tests()
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failed_tests(patch, run_id, chunk_number=0):
        return make_test_result(run_id, False)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert "failed" in event_statuses(run_id, "chunk_status_changed")
    assert "failed" in event_statuses(run_id, "run_status_changed")


@pytest.mark.asyncio
async def test_broken_event_bus_does_not_break_happy_path_execution(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(
        chunked_orchestrator.event_bus,
        "publish",
        lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"


@pytest.mark.asyncio
async def test_broken_event_bus_does_not_break_high_risk_pause(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(
        tmp_repo,
        tracked_runs,
        review_chunks={1},
    )
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(
        chunked_orchestrator.event_bus,
        "publish",
        lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_chunk_approval"
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "awaiting_chunk_approval"


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


def test_completion_summary_classifies_edit_as_modified():
    chunk = ChunkDefinition(
        chunk_number=1,
        title="Fix typo",
        description="Correct a README typo",
        token_estimate=10,
    )
    plan = make_planner_result("run-summary")
    coder_output = CoderHandoff(
        run_id="run-summary",
        feature_description="fix typo",
        files_changed=[
            FileChange(
                path="README.md",
                action="edit",
                old_string="waht si this englsh?",
                new_string="what is this english?",
                reason="fix typo",
            ),
            FileChange(
                path="new.py",
                action="create",
                content="x = 1\n",
                reason="new file",
            ),
            FileChange(
                path="old.py",
                action="modify",
                content="y = 2\n",
                reason="modify file",
            ),
            FileChange(
                path="gone.py",
                action="delete",
                content=None,
                reason="delete file",
            ),
        ],
        summary="Mixed actions",
    )

    summary = chunked_orchestrator._build_completion_summary(
        chunk, plan, coder_output
    )

    # Targeted edit is counted as a modification, alongside modify.
    assert summary["files_modified"] == ["README.md", "old.py"]
    assert summary["files_created"] == ["new.py"]
    assert summary["files_deleted"] == ["gone.py"]


# --- PR #15A: AI memory suggestions must never auto-promote to long-term memory ---
# make_coder_result() emits suggested_memory_entries=["remember chunk"]. A run
# (failed, unapproved, or even fully successful) may surface these as advisory
# suggestions, but it must never insert them into memory_facts. Only an explicit
# human create/approve through the Memory API writes long-term memory.


@pytest.mark.asyncio
async def test_failed_chunk_run_writes_no_long_term_memory(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failed_tests(patch, run_id, chunk_number=0):
        return make_test_result(run_id, False)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    # The coder produced suggested_memory_entries, but the failed run must not
    # auto-promote them into memory_facts.
    assert count_memory_facts(project["id"]) == 0


@pytest.mark.asyncio
async def test_unapproved_run_writes_no_long_term_memory(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs, approved=False)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.execute_approved_chunks(run_id)

    # A run that was never approved (the rejected/cancelled-equivalent path)
    # never executes the coder and must leave memory untouched.
    assert count_memory_facts(project["id"]) == 0


@pytest.mark.asyncio
async def test_successful_chunk_run_does_not_auto_promote_memory(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    # Even on a successful chunk, AI suggestions stay advisory: they are recorded
    # in the chunk completion summary for human review, never written to memory.
    assert count_memory_facts(project["id"]) == 0

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT completion_summary FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
    summary = json.loads(row[0])
    assert summary["suggested_memory_entries"] == ["remember chunk"]


@pytest.mark.asyncio
async def test_all_chunks_complete_marks_run_awaiting_final_approval(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert result["completed_chunks"] == 2
    assert result["final_approval_required"] is True
    status = get_chunk_plan_status(run_id)
    assert all(chunk.status == "completed" for chunk in status.chunks)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, current_chunk_number
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "awaiting_final_approval"
    assert row[1] == 2


@pytest.mark.asyncio
async def test_execute_creates_final_approval_gate(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT approval_type, chunk_number, status
            FROM approval_gates
            WHERE run_id = :run_id AND approval_type = 'final'
        """), {"run_id": run_id}).fetchone()
        count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert row is not None
    assert row[0] == "final"
    assert row[1] == 0
    assert row[2] == "pending"
    assert count == 1


@pytest.mark.asyncio
async def test_final_approval_requires_clean_worktree_after_chunks(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    clean_checks = {"count": 0}
    monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_git_repo", lambda repo: None)

    def ensure_clean(repo):
        clean_checks["count"] += 1
        if clean_checks["count"] > 1:
            raise RuntimeError("[GIT] dirty after chunks")

    monkeypatch.setattr(chunked_orchestrator.local_git, "ensure_clean_worktree", ensure_clean)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "create_or_checkout_branch",
        lambda branch, repo: calls.append(("branch", branch, repo)),
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        lambda files, message, repo: calls.append(("commit", files, message, repo)),
    )
    patch_success_pipeline(monkeypatch, run_id, calls)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.execute_approved_chunks(run_id)

    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_final_approval_gate_is_idempotent(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)
    await chunked_orchestrator.execute_approved_chunks(run_id)

    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 1


def test_scope_guard_no_final_approval_push_or_pr():
    paths = {route.path for route in app.routes}
    assert not hasattr(chunked_orchestrator, "create_pull_request")
    assert not hasattr(chunked_orchestrator.local_git, "push_was_called")
    assert not hasattr(chunked_orchestrator, "final_approval")


def test_pipeline_uses_publish_safe_wrapper_for_events():
    source = inspect.getsource(chunked_orchestrator)

    assert "def _publish_safe" in source
    assert source.count("event_bus.publish(") == 1


def test_execute_route_calls_execute_approved_chunks(monkeypatch):
    called = {"run_id": None}

    async def fake_execute(run_id):
        called["run_id"] = run_id
        return {"status": "awaiting_final_approval", "run_id": run_id}

    monkeypatch.setattr("backend.routes.chunks.execute_approved_chunks", fake_execute)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/execute")

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_final_approval"
    assert called["run_id"] == "run-123"


def test_approve_chunk_commits_from_code_checkpoint_without_rerunning(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    add_code_checkpoint(run_id, 1)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)

    async def fail_planner(*args, **kwargs):
        raise AssertionError("planner must not run")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fail_planner)

    result = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)

    assert result["status"] == "chunk_approved"
    assert result["chunk_number"] == 1
    assert any(call[0] == "commit" for call in calls)
    commit_call = next(call for call in calls if call[0] == "commit")
    assert commit_call[1] == ["created_1.py", "modified_1.py", "deleted_1.py"]
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ag.status, pr.status
            FROM approval_gates ag
            JOIN pipeline_runs pr ON pr.id = ag.run_id
            WHERE ag.run_id = :run_id
              AND ag.approval_type = 'chunk'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "approved"
    assert row[1] == "chunk_approved"


@pytest.mark.asyncio
async def test_resume_after_chunk_approval_continues_next_pending_chunk(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(
        tmp_repo,
        tracked_runs,
        chunks=2,
        review_chunks={1},
    )
    add_code_checkpoint(run_id, 1)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)

    approve_result = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)

    assert approve_result["status"] == "chunk_approved"
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert any(call[0] == "planner" and call[1] == 2 for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"
    assert status.chunks[1].status == "completed"


@pytest.mark.asyncio
async def test_resume_after_last_chunk_approval_creates_final_gate(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    add_code_checkpoint(run_id, 1)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    chunked_orchestrator.approve_chunk_and_commit(run_id, 1)
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert not any(call[0] == "planner" for call in calls)
    with engine.connect() as conn:
        final_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert final_count == 1


def test_reject_chunk_rolls_back_and_fails_without_commit(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    rollback_calls = []
    monkeypatch.setattr(
        chunked_orchestrator,
        "rollback_patch",
        lambda run_id, chunk_number=0: rollback_calls.append((run_id, chunk_number)) or True,
    )

    result = chunked_orchestrator.reject_chunk_and_rollback(
        run_id,
        1,
        "not safe",
    )

    assert result["status"] == "chunk_rejected"
    assert rollback_calls == [(run_id, 1)]
    assert not any(call[0] == "commit" for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "rejected"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ag.status, ag.rejection_reason, pr.status
            FROM approval_gates ag
            JOIN pipeline_runs pr ON pr.id = ag.run_id
            WHERE ag.run_id = :run_id
              AND ag.approval_type = 'chunk'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "rejected"
    assert row[1] == "not safe"
    assert row[2] == "failed"


def test_reject_chunk_verifies_clean_worktree_after_rollback(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    clean_checks = []
    monkeypatch.setattr(
        chunked_orchestrator,
        "rollback_patch",
        lambda run_id, chunk_number=0: True,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: clean_checks.append(repo),
    )

    result = chunked_orchestrator.reject_chunk_and_rollback(run_id, 1)

    assert result["status"] == "chunk_rejected"
    assert clean_checks == [str(tmp_repo)]


def test_reject_chunk_fails_without_success_status_when_rollback_fails(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    monkeypatch.setattr(
        chunked_orchestrator,
        "rollback_patch",
        lambda run_id, chunk_number=0: False,
    )

    with pytest.raises(RuntimeError, match="rollback failed or was unavailable"):
        chunked_orchestrator.reject_chunk_and_rollback(run_id, 1)

    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "awaiting_chunk_approval"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ag.status, pr.status
            FROM approval_gates ag
            JOIN pipeline_runs pr ON pr.id = ag.run_id
            WHERE ag.run_id = :run_id
              AND ag.approval_type = 'chunk'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "pending"
    assert row[1] != "failed"


def test_reject_chunk_fails_if_worktree_remains_dirty_after_rollback(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    monkeypatch.setattr(
        chunked_orchestrator,
        "rollback_patch",
        lambda run_id, chunk_number=0: True,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: (_ for _ in ()).throw(RuntimeError("[GIT] dirty")),
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_dirty_files",
        lambda repo: ["src/advanced_math.py", "tests/test_advanced_math.py"],
    )

    with pytest.raises(RuntimeError, match="rollback did not clean worktree"):
        chunked_orchestrator.reject_chunk_and_rollback(run_id, 1)

    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "awaiting_chunk_approval"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ag.status, pr.status
            FROM approval_gates ag
            JOIN pipeline_runs pr ON pr.id = ag.run_id
            WHERE ag.run_id = :run_id
              AND ag.approval_type = 'chunk'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "pending"
    assert row[1] != "failed"


@pytest.mark.asyncio
async def test_resume_refuses_when_run_missing():
    with pytest.raises(Exception):
        await chunked_orchestrator.resume_chunked_pipeline("missing-run")


@pytest.mark.asyncio
async def test_resume_refuses_when_chunk_plan_not_approved(tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, approved=False)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.resume_chunked_pipeline(run_id)


@pytest.mark.asyncio
async def test_resume_checks_out_existing_branch(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_resume_git(monkeypatch, calls)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "assert_not_on_stale_pipewright_branch",
        lambda repo, run_id: (_ for _ in ()).throw(
            AssertionError("stale branch guard must not run during resume")
        ),
    )
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert ("run_git", ["checkout", f"pipewright/{run_id[:8]}"], project["repo_path"]) in calls


@pytest.mark.asyncio
async def test_resume_fails_if_expected_branch_missing(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_resume_git(monkeypatch, calls, branch_exists=False)

    with pytest.raises(RuntimeError):
        await chunked_orchestrator.resume_chunked_pipeline(run_id)


@pytest.mark.asyncio
async def test_resume_fails_if_worktree_dirty(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_resume_git(monkeypatch, calls, clean=False)

    with pytest.raises(RuntimeError) as error:
        await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert "Manual cleanup or rollback is required" in str(error.value)


@pytest.mark.asyncio
async def test_resume_with_awaiting_chunk_approval_does_not_rerun(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_resume_git(monkeypatch, calls)

    async def fail_planner(*args, **kwargs):
        raise AssertionError("planner must not run")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fail_planner)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_chunk_approval"
    assert result["chunk_number"] == 1
    assert not any(call[0] == "planner" for call in calls)


@pytest.mark.asyncio
async def test_resume_resets_stale_running_chunks_to_pending(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    update_chunk_status(run_id, 1, "running")
    patch_resume_git(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    await chunked_orchestrator.resume_chunked_pipeline(run_id)

    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"


@pytest.mark.asyncio
async def test_resume_skips_chunk_with_test_checkpoint(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["skipped_chunks"] == 1
    assert not any(call[0] == "planner" for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"


@pytest.mark.asyncio
async def test_resume_reruns_chunk_without_test_checkpoint(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert any(call[0] == "planner" and call[1] == 1 for call in calls)
    assert any(call[0] == "commit" for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"


@pytest.mark.asyncio
async def test_resume_after_rejected_chunk_reruns_without_reusing_gate(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    update_chunk_status(run_id, 1, "rejected", "not safe")
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO approval_gates
            (id, run_id, step, status, approval_type, chunk_number)
            VALUES (:id, :run_id, 'chunk-approval', 'rejected', 'chunk', 1)
        """), {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
        })
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_chunk_approval"
    assert any(call[0] == "planner" and call[1] == 1 for call in calls)
    with engine.connect() as conn:
        pending_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'chunk'
              AND chunk_number = 1
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert pending_count == 1


@pytest.mark.asyncio
async def test_resume_processes_chunks_in_order_and_stops_after_failure(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=3)
    add_test_checkpoint(run_id, 1)
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failed_tests(patch, run_id, chunk_number=0):
        calls.append(("test", chunk_number))
        return make_test_result(run_id, chunk_number != 2)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert not any(call[0] == "planner" and call[1] == 3 for call in calls)


@pytest.mark.asyncio
async def test_test_failure_during_resume_marks_failed(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failed_tests(patch, run_id, chunk_number=0):
        calls.append(("test", chunk_number))
        return make_test_result(run_id, False)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert not any(call[0] == "planner" and call[1] == 2 for call in calls)
    with engine.connect() as conn:
        status = conn.execute(text(
            "SELECT status FROM pipeline_runs WHERE id = :run_id"
        ), {"run_id": run_id}).fetchone()[0]
    assert status == "failed"


@pytest.mark.asyncio
async def test_commit_failure_during_resume_marks_failed(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def fail_commit(files, message, repo):
        raise RuntimeError("[GIT] commit failed")

    monkeypatch.setattr(chunked_orchestrator.local_git, "commit_files", fail_commit)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert not any(call[0] == "planner" and call[1] == 2 for call in calls)


@pytest.mark.asyncio
async def test_resume_repairs_stale_db_state(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    update_chunk_status(run_id, 1, "running")
    add_test_checkpoint(run_id, 1)
    patch_resume_git(monkeypatch)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["skipped_chunks"] == 1
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"


@pytest.mark.asyncio
async def test_resume_creates_final_approval_gate(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    patch_resume_git(monkeypatch)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_final_approval"
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 1


@pytest.mark.asyncio
async def test_resume_fails_if_commit_missing_despite_test_checkpoint(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    patch_resume_git(monkeypatch)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_message_exists",
        lambda repo, prefix: False,
    )

    with pytest.raises(RuntimeError) as error:
        await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert "unsafe resume recovery" in str(error.value)


def test_resume_route_calls_resume_chunked_pipeline(monkeypatch):
    called = {"run_id": None}

    async def fake_resume(run_id):
        called["run_id"] = run_id
        return {
            "status": "awaiting_final_approval",
            "run_id": run_id,
            "resumed": True,
        }

    monkeypatch.setattr("backend.routes.chunks.resume_chunked_pipeline", fake_resume)
    client = TestClient(app)

    response = client.post("/runs/run-123/chunks/resume")

    assert response.status_code == 200
    assert response.json()["resumed"] is True
    assert called["run_id"] == "run-123"


@pytest.mark.asyncio
async def test_scope_drift_fails_chunk_before_apply_patch_or_commit(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)

    async def fake_planner(*args, **kwargs):
        return make_planner_result(run_id)

    async def fake_coder(*args, **kwargs):
        return CoderHandoff(
            run_id=run_id,
            feature_description="enriched",
            files_changed=[
                FileChange(
                    path="src/out_of_scope.py",
                    action="modify",
                    content="print('drift')\n",
                    reason="drift",
                ),
            ],
            summary="drifted",
            suggested_memory_entries=[],
        )

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)
    monkeypatch.setattr(
        chunked_orchestrator,
        "apply_patch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("apply_patch must not be called on scope drift")
        ),
    )
    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("run_tests must not be called on scope drift")
        ),
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert "src/out_of_scope.py" in result["error"]
    assert "files_expected" in result["error"]
    assert not any(call[0] == "commit" for call in calls)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, error_message
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "failed"
    assert "src/out_of_scope.py" in (row[1] or "")


@pytest.mark.asyncio
async def test_db_conflict_policy_runs_once_and_does_not_block(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    # #16D-4: the DB-conflict policy is evaluated once per execute (not per chunk).
    # When it returns no pause, execution proceeds normally.
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    calls = {"count": 0}

    def spy(run_id_arg, project_id, repo_path, files_expected):
        calls["count"] += 1

    monkeypatch.setattr(chunked_orchestrator, "_apply_db_memory_conflict_policy", spy)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    # Evaluated exactly once across a 2-chunk run, and the run proceeded normally.
    assert calls["count"] == 1
    assert result["status"] == "awaiting_final_approval"
