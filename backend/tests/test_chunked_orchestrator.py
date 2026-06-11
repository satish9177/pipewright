"""
test_chunked_orchestrator.py
Tests for Phase 2B-4A sequential chunk execution.
No real AI calls, no real GitHub, no push.
"""

import json
import uuid
import inspect
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.events.event_bus import clear_all_events_for_tests, get_buffered_events
from backend.main import app
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.git.local_git import StartBranchInspection
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
    get_chunk_test_run_verdict,
    save_chunk_completion_summary,
    update_chunk_status,
)
from backend.pipeline.patch_applier import PatchApplyOutcome
from backend.pipeline.patch_dry_run import DryRunResult
from backend.pipeline.patch_failures import (
    PATCH_FAILURE_KIND,
    RECOVERED_PATCH_REVIEW_KIND,
    RETRY_INELIGIBLE_CAP_EXHAUSTED,
    RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET,
    RETRY_INELIGIBLE_DIRTY_WORKTREE,
    RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE,
    RETRY_INELIGIBLE_MISSING_REPORT,
    RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID,
    RETRY_INELIGIBLE_WRONG_BRANCH,
    PatchFailureType,
    build_patch_failure_report,
    default_message_for_failure_type,
    patch_failure_report_to_completion_summary,
    record_initial_attempt,
    record_retry_attempt,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


# Stateful working-tree model for fakes (#18D). The new clean-tree precondition
# at the start of each chunk requires a clean tree, while the no-effective-change
# commit guard requires a dirty tree at commit time. A single constant cannot be
# both, so fakes track whether a patch has been applied this chunk: clean before
# apply, dirty after apply, clean again after commit.
_worktree_applied = {"value": False}
START_BRANCH = "feature/start"
START_SHA = "abcdef1234567890abcdef1234567890abcdef1234"
OTHER_SHA = "1111111111111111111111111111111111111111"


def reset_worktree_state() -> None:
    _worktree_applied["value"] = False


def mark_worktree_applied() -> None:
    # Model a chunk whose patch is already applied on disk (e.g. a paused
    # high-risk chunk being approved), so the working tree reads dirty at commit.
    _worktree_applied["value"] = True


def _stateful_is_clean(repo_path) -> bool:
    return not _worktree_applied["value"]


def make_guarded_apply(calls=None):
    """Return a fake apply_patch_guarded that records a successful apply."""

    def fake_guarded(code, run_id, chunk_number=0, *, files_expected, repo_path=None):
        if calls is not None:
            calls.append(("patch", chunk_number))
        _worktree_applied["value"] = True
        return PatchApplyOutcome.from_success(make_patch_result(run_id))

    return fake_guarded


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


def set_run_start_context(
    run_id: str,
    *,
    branch: str | None = START_BRANCH,
    head_sha: str | None = START_SHA,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET start_branch = :branch,
                start_head_sha = :head_sha
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "branch": branch,
            "head_sha": head_sha,
        })


def run_status(run_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()[0]


def patch_start_inspection(
    monkeypatch,
    *,
    branch: str | None = START_BRANCH,
    head_sha: str | None = START_SHA,
    error: str | None = None,
) -> None:
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "inspect_start_branch",
        lambda _repo: StartBranchInspection(
            current_branch=branch,
            head_sha=head_sha,
            head_sha_short=head_sha[:12] if head_sha else None,
            is_detached=branch is None,
            error=error,
        ),
    )


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


def patch_git_preflight(monkeypatch, calls=None, run_id=None):
    # #26D3a: when a retry run_id is given, model HEAD already on the run branch so
    # the verify-only branch pre-check passes. Patched only when run_id is set so
    # non-retry tests keep real get_current_branch (also used by pr_orchestrator).
    if run_id is not None:
        monkeypatch.setattr(
            chunked_orchestrator.local_git,
            "get_current_branch",
            lambda repo: f"pipewright/{run_id[:8]}",
        )
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
    def fake_commit_files(files, message, repo):
        # Committing makes the tree clean again for the next chunk.
        reset_worktree_state()
        if calls is not None:
            calls.append(("commit", files, message, repo))
        return "hash"

    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_files",
        fake_commit_files,
    )
    # Working tree starts clean; a fake apply marks it dirty; a fake commit
    # marks it clean again. This lets the #18D clean-tree precondition and the
    # no-effective-change commit guard both behave correctly in fakes.
    reset_worktree_state()
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "is_working_tree_clean",
        _stateful_is_clean,
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

    def fake_dry(code, repo_path):
        if calls is not None:
            calls.append(("dry_run", None))
        return DryRunResult(ok=True)

    def fake_tests(patch, run_id, chunk_number=0):
        if calls is not None:
            calls.append(("test", chunk_number))
        return make_test_result(run_id, True)

    reset_worktree_state()
    monkeypatch.setattr(chunked_orchestrator, "run_planner", fake_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)
    monkeypatch.setattr(chunked_orchestrator, "dry_run_changes", fake_dry)
    monkeypatch.setattr(
        chunked_orchestrator, "apply_patch_guarded", make_guarded_apply(calls)
    )
    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)
    # Stateful working-tree fake: clean before apply, dirty after, clean after
    # commit. Keeps both the clean-tree precondition and the no-effective-change
    # commit guard correct without a constant that can only model one moment.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "is_working_tree_clean",
        _stateful_is_clean,
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
        "apply_patch_guarded",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("apply_patch_guarded should not be called")
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

    expected_message = default_message_for_failure_type(PatchFailureType.NO_CHANGES)
    assert result["status"] == "failed"
    assert result["error"] == expected_message
    assert not any(call[0] == "commit" for call in calls)
    with engine.connect() as conn:
        chunk = conn.execute(text("""
            SELECT status, error_message, completion_summary
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
        run = conn.execute(text("""
            SELECT status, current_step
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert chunk[0] == "failed"
    assert chunk[1] == expected_message
    summary = json.loads(chunk[2])
    assert summary["kind"] == PATCH_FAILURE_KIND
    assert summary["failure_type"] == PatchFailureType.NO_CHANGES.value
    assert run[0] == "failed"
    assert run[1] == "chunk_1_failed"


def test_commit_and_complete_chunk_refuses_empty_touched_files(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
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
            project["id"],
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
    run_id, project = create_run(tmp_repo, tracked_runs)
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
            project["id"],
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
async def test_start_context_no_drift_allows_branch_creation(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_start_inspection(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    assert ("branch", f"pipewright/{run_id[:8]}", project["repo_path"]) in calls


@pytest.mark.asyncio
async def test_start_context_allows_current_runs_own_branch(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_start_inspection(
        monkeypatch,
        branch=f"pipewright/{run_id[:8]}",
        head_sha=OTHER_SHA,
    )
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    assert ("branch", f"pipewright/{run_id[:8]}", project["repo_path"]) in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_branch", "current_head_sha"),
    [
        ("feature/other", START_SHA),
        (START_BRANCH, OTHER_SHA),
        (None, START_SHA),
        ("pipewright/old-run", START_SHA),
    ],
)
async def test_start_context_drift_blocks_before_branch_creation(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    current_branch,
    current_head_sha,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_start_inspection(
        monkeypatch,
        branch=current_branch,
        head_sha=current_head_sha,
    )
    _forbid_execution(monkeypatch)
    _forbid_branch_switch(monkeypatch)
    before_status = run_status(run_id)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "start_context_drifted"
    assert result["status_code"] == 409
    assert result["captured_start"] == {
        "branch": START_BRANCH,
        "head_sha_short": START_SHA[:12],
    }
    assert result["current"] == {
        "branch": current_branch,
        "head_sha_short": current_head_sha[:12] if current_head_sha else None,
    }
    assert "Checkout feature/start" in result["message"]
    assert run_status(run_id) == before_status
    assert not any(call[0] == "branch" for call in calls)


@pytest.mark.asyncio
async def test_start_context_inspection_error_blocks_without_mutation(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_start_inspection(
        monkeypatch,
        branch=None,
        head_sha=None,
        error="git failed",
    )
    _forbid_execution(monkeypatch)
    _forbid_branch_switch(monkeypatch)
    before_status = run_status(run_id)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "start_context_drifted"
    assert result["status_code"] == 409
    assert result["current"] == {
        "branch": None,
        "head_sha_short": None,
    }
    assert run_status(run_id) == before_status
    assert not any(call[0] == "branch" for call in calls)


@pytest.mark.asyncio
async def test_null_start_context_skips_drift_check(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "inspect_start_branch",
        lambda _repo: (_ for _ in ()).throw(
            AssertionError("legacy null start context must skip drift check")
        ),
    )
    patch_success_pipeline(monkeypatch, run_id, calls)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    assert ("branch", f"pipewright/{run_id[:8]}", project["repo_path"]) in calls


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

    # Chunk 2 was the last outstanding chunk, so approving it completes the plan
    # and advances the run straight to final approval (#44A).
    assert approve_result["status"] == "awaiting_final_approval"
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
    mark_worktree_applied()  # the paused chunk's patch is already on disk

    async def fail_planner(*args, **kwargs):
        raise AssertionError("planner must not run")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fail_planner)

    result = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)

    # This was the only chunk, so approving it completes the plan and advances the
    # run straight to final approval (#44A) without rerunning the planner.
    assert result["status"] == "awaiting_final_approval"
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
    assert row[1] == "awaiting_final_approval"


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
    mark_worktree_applied()  # the paused chunk's patch is already on disk

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


def test_last_chunk_approval_advances_to_final_approval(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    """#44A regression: approving the final outstanding chunk must surface final
    approval on its own — the run must NOT stall at chunk_approved waiting for a
    resume that has no remaining work and no UI affordance.

    Exact case from the smoke: one chunk, chunk completed + committed, chunk
    approved, all chunks complete. Approval must move the run to
    awaiting_final_approval and create exactly one PENDING final gate, without
    rerunning the planner and without auto-approving (the gate stays pending).
    """
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    add_code_checkpoint(run_id, 1)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    from backend.pipeline.approval_gate import create_chunk_approval_gate
    create_chunk_approval_gate(run_id, 1, "chunk approval")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    mark_worktree_applied()  # the paused chunk's patch is already on disk

    async def fail_planner(*args, **kwargs):
        raise AssertionError("planner must not run on chunk approval")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", fail_planner)

    result = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)

    assert result["status"] == "awaiting_final_approval"
    assert result["chunk_number"] == 1
    assert not any(call[0] == "planner" for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"
    with engine.connect() as conn:
        run_status = conn.execute(text("""
            SELECT status FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()[0]
        final_count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert run_status == "awaiting_final_approval"
    # Exactly one PENDING final gate — surfaced for a human decision, never
    # auto-approved.
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
async def test_resume_does_not_verify_start_context(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id, branch=START_BRANCH, head_sha=START_SHA)
    calls = []
    patch_resume_git(monkeypatch, calls)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "inspect_start_branch",
        lambda _repo: (_ for _ in ()).throw(
            AssertionError("resume must not run start-context drift check")
        ),
    )
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert any(call[0] == "planner" and call[1] == 1 for call in calls)


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
        "apply_patch_guarded",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("apply_patch_guarded must not be called on scope drift")
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

    # The structured report carries the user-facing message; the offending path
    # and scope detail move into completion_summary, not error_message.
    assert result["status"] == "failed"
    assert result["error"] == default_message_for_failure_type(
        PatchFailureType.SCOPE_VIOLATION
    )
    assert not any(call[0] == "commit" for call in calls)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, error_message, completion_summary
            FROM chunks
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "failed"
    summary = json.loads(row[2])
    assert summary["kind"] == PATCH_FAILURE_KIND
    assert summary["failure_type"] == PatchFailureType.SCOPE_VIOLATION.value
    assert "src/out_of_scope.py" in summary["changed_files_attempted"]
    assert "src/out_of_scope.py" in (summary.get("technical_details") or "")


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


# --------------------------------------------------------------------------- #
# Structured patch failure wiring (#18D)
# --------------------------------------------------------------------------- #


def _read_completion_summary(run_id: str, chunk_number: int = 1) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT completion_summary FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()
    assert row is not None and row[0] is not None
    return json.loads(row[0])


@pytest.mark.asyncio
async def test_guarded_apply_failure_persists_report_and_stops(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details="hunk #2 FAILED",
        changed_files_attempted=["created_1.py"],
        allowed_files=["created_1.py"],
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="patch",
    )

    def fake_guarded_fail(code, run_id, chunk_number=0, *, files_expected, repo_path=None):
        calls.append(("patch", chunk_number))
        return PatchApplyOutcome.from_failure(report)

    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", fake_guarded_fail)
    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_tests should not run after a failed apply")
        ),
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert result["error"] == report.message
    # Stops: no commit, chunk 2 never coded.
    assert not any(c[0] == "commit" for c in calls)
    assert not any(c[0] == "coder" and c[1] == 2 for c in calls)

    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "failed"
    assert status.chunks[0].error_message == report.message

    summary = _read_completion_summary(run_id, 1)
    assert summary["kind"] == PATCH_FAILURE_KIND
    assert summary["failure_type"] == PatchFailureType.PATCH_DOES_NOT_APPLY.value
    assert summary["technical_details"] == "hunk #2 FAILED"
    assert summary["changed_files_attempted"] == ["created_1.py"]


@pytest.mark.asyncio
async def test_guarded_apply_failure_emits_slim_stage_failed_event(
    monkeypatch, tmp_repo, tracked_runs
):
    clear_all_events_for_tests()
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    report = build_patch_failure_report(
        PatchFailureType.SCOPE_VIOLATION,
        technical_details="x" * 5000,  # large details must NOT bloat the event
        changed_files_attempted=["sneaky.py"],
        allowed_files=["allowed.py"],
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
    )

    def fake_guarded_fail(code, run_id, chunk_number=0, *, files_expected, repo_path=None):
        return PatchApplyOutcome.from_failure(report)

    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", fake_guarded_fail)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    failed_events = [
        e for e in get_buffered_events(run_id) if e.kind == "stage_failed"
    ]
    assert failed_events, "expected a stage_failed event"
    event = failed_events[-1]
    assert event.stage == "patch"
    assert event.level == "error"
    assert event.data.get("kind") == "patch_failure"
    assert event.data.get("failure_type") == PatchFailureType.SCOPE_VIOLATION.value
    # Slim payload: full technical_details never go into the event.
    assert "technical_details" not in event.data
    assert event.data.get("truncated") is not True
    assert event.data["changed_files_attempted_count"] == 1


@pytest.mark.asyncio
async def test_dirty_worktree_fails_fast_before_coder(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    # Preflight (ensure_clean_worktree) is faked clean; force the per-chunk
    # precondition to see a dirty tree.
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: False
    )

    flags = {"planner": False, "coder": False, "guarded": False, "tests": False}

    async def rec_planner(*a, **k):
        flags["planner"] = True
        return make_planner_result(run_id)

    async def rec_coder(*a, **k):
        flags["coder"] = True
        return make_coder_result(run_id, 1)

    def rec_guarded(*a, **k):
        flags["guarded"] = True
        return PatchApplyOutcome.from_success(make_patch_result(run_id))

    def rec_tests(*a, **k):
        flags["tests"] = True
        return make_test_result(run_id, True)

    monkeypatch.setattr(chunked_orchestrator, "run_planner", rec_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", rec_coder)
    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", rec_guarded)
    monkeypatch.setattr(chunked_orchestrator, "run_tests", rec_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["error"] == default_message_for_failure_type(
        PatchFailureType.DIRTY_WORKTREE
    )
    assert flags == {
        "planner": False,
        "coder": False,
        "guarded": False,
        "tests": False,
    }
    assert not any(c[0] == "commit" for c in calls)
    summary = _read_completion_summary(run_id, 1)
    assert summary["failure_type"] == PatchFailureType.DIRTY_WORKTREE.value


@pytest.mark.asyncio
async def test_test_failure_after_apply_reports_and_does_not_double_rollback(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda patch, run_id, chunk_number=0: make_test_result(run_id, False),
    )
    # tester.py owns rollback on test failure; the orchestrator must not also
    # roll back.
    monkeypatch.setattr(
        chunked_orchestrator,
        "rollback_patch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("orchestrator must not roll back; tester already did")
        ),
    )
    # Simulate the tester having rolled back to a clean tree.
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: True
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert not any(c[0] == "commit" for c in calls)
    summary = _read_completion_summary(run_id, 1)
    assert summary["failure_type"] == PatchFailureType.TEST_FAILURE_AFTER_APPLY.value
    assert summary["rollback_performed"] is True
    assert summary["working_tree_clean"] is True
    assert summary["failed_step"] == "test"


@pytest.mark.asyncio
async def test_success_path_writes_no_patch_failure_summary(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert any(c[0] == "commit" for c in calls)
    summary = _read_completion_summary(run_id, 1)
    # A successful chunk keeps its normal success summary, never a failure one.
    assert summary.get("kind") != PATCH_FAILURE_KIND
    assert "chunk_title" in summary


# ---------------------------------------------------------------------------
# E8 symmetry: the main path surfaces files_expected (planner prompt + coder
# plan) and runs the zero-mutation dry-run pre-flight before apply, exactly
# like the human-retry path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_path_surfaces_files_expected_to_planner_and_coder(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    captured_plans = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    async def capture_coder(plan, run_id_arg, chunk_number=0, **kwargs):
        captured_plans.append(plan)
        return make_coder_result(run_id_arg, chunk_number)

    monkeypatch.setattr(chunked_orchestrator, "run_coder", capture_coder)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    # The planner prompt context names every approved file...
    planner_calls = [c for c in calls if c[0] == "planner"]
    assert planner_calls
    enriched = planner_calls[0][2]
    for path in ("created_1.py", "modified_1.py", "deleted_1.py"):
        assert path in enriched
    # ...and the plan handed to the coder surfaces them via files_to_modify
    # (prompt context only — write scope stays files_expected).
    plan = captured_plans[0]
    for path in ("created_1.py", "modified_1.py", "deleted_1.py"):
        assert path in plan.files_to_modify


@pytest.mark.asyncio
async def test_main_path_dry_run_runs_before_apply(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    names = [c[0] for c in calls]
    assert "dry_run" in names
    assert "patch" in names
    assert names.index("dry_run") < names.index("patch")


@pytest.mark.asyncio
async def test_main_path_dry_run_failure_prevents_apply(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def failing_dry(code, repo_path):
        calls.append(("dry_run", None))
        return DryRunResult(
            ok=False,
            failed_path="modified_1.py",
            failed_action="edit",
            error_message=(
                "patch_applier.py: edit old_string not found in modified_1.py. "
                "The text to replace must match the file exactly."
            ),
        )

    monkeypatch.setattr(chunked_orchestrator, "dry_run_changes", failing_dry)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    # Dry-run gates apply/test/commit entirely in the main path too.
    assert not any(c[0] in {"patch", "test", "commit"} for c in calls)
    assert _chunk_status_value(run_id, 1) == "failed"
    summary = _read_completion_summary(run_id, 1)
    assert summary["kind"] == PATCH_FAILURE_KIND
    assert summary["failure_type"] == PatchFailureType.PATCH_DOES_NOT_APPLY.value
    assert summary["failed_step"] == "patch"


# ---------------------------------------------------------------------------
# #24A: chunk dependency execution enforcement.
#
# A chunk may begin execution only if every chunk in its depends_on is
# completed. make_triage wires depends_on=[number-1], so chunk 2 depends on
# chunk 1. These tests assert the guard fires before any planner/coder/patch/
# commit work, on both fresh execute and resume (including the checkpoint-skip
# path), and that valid sequential runs are unchanged.
# ---------------------------------------------------------------------------


def _chunk_status_value(run_id: str, chunk_number: int) -> str:
    status = get_chunk_plan_status(run_id)
    return next(
        chunk.status
        for chunk in status.chunks
        if chunk.chunk_number == chunk_number
    )


def test_unmet_dependencies_helper_only_completed_satisfies():
    chunk = ChunkDefinition(
        chunk_number=2,
        title="Chunk 2",
        description="Do chunk 2",
        files_expected=["f.py"],
        depends_on=[1],
        risk_level="low",
        token_estimate=10,
        requires_human_review=False,
        rationale="dep test",
    )
    assert chunked_orchestrator._unmet_dependencies(chunk, {1: "completed"}) == []
    for status in ("pending", "running", "failed", "rejected", "awaiting_chunk_approval"):
        assert chunked_orchestrator._unmet_dependencies(chunk, {1: status}) == [1]
    # Missing reference fails safe.
    assert chunked_orchestrator._unmet_dependencies(chunk, {}) == [1]


# A. Dependency satisfied → allowed.
@pytest.mark.asyncio
async def test_dependency_satisfied_allows_dependent_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    status = get_chunk_plan_status(run_id)
    assert [chunk.status for chunk in status.chunks] == ["completed", "completed"]
    assert all(
        "DEPENDENCY_NOT_MET" not in (chunk.error_message or "")
        for chunk in status.chunks
    )


# B. Dependency failed → blocked.
@pytest.mark.asyncio
async def test_failed_dependency_blocks_dependent_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    update_chunk_status(run_id, 1, "failed", "boom")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in result["error"]
    assert not any(
        call[0] in {"planner", "coder", "patch", "commit"} for call in calls
    )
    assert _chunk_status_value(run_id, 2) == "failed"
    assert "DEPENDENCY_NOT_MET" in get_chunk_plan_status(run_id).chunks[1].error_message


# C. Dependency pending/not-started → blocked (direct check on the choke point).
@pytest.mark.asyncio
async def test_pending_dependency_blocks_dependent_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs, chunks=2)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    plan = get_chunk_plan_status(run_id)
    definitions = chunked_orchestrator._definition_by_number(plan)
    result = await chunked_orchestrator._execute_single_chunk(
        run_id,
        project["id"],
        definitions[2],
        project["repo_path"],
        f"pipewright/{run_id[:8]}",
        {1: "pending", 2: "pending"},
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in result["error"]
    assert "chunk 1 status: pending" in result["error"]
    assert not any(call[0] in {"planner", "coder", "patch"} for call in calls)


# D. High-risk approval pause re-entry bypass regression.
@pytest.mark.asyncio
async def test_reexecute_during_chunk_approval_pause_cannot_skip_dependency(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(
        tmp_repo, tracked_runs, chunks=2, review_chunks={1}
    )
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    first = await chunked_orchestrator.execute_approved_chunks(run_id)
    assert first["status"] == "awaiting_chunk_approval"
    assert first["chunk_number"] == 1

    # Chunk 1 is paused at awaiting_chunk_approval (NOT completed). Re-entering
    # execution must not run chunk 2 (depends_on=[1]) behind the paused dep.
    calls.clear()
    second = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert second["status"] == "failed"
    assert second["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in second["error"]
    assert "awaiting_chunk_approval" in second["error"]
    assert not any(
        call[0] in {"planner", "coder", "patch", "commit"} for call in calls
    )
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "awaiting_chunk_approval"
    assert status.chunks[1].status == "failed"


# E. Resume with an incomplete dependency must not execute the dependent.
@pytest.mark.asyncio
async def test_resume_blocks_dependent_when_dependency_incomplete(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    # Chunk 1 incomplete but excluded from the resumable set (awaiting approval
    # with no pending gate, so resume does not early-return on it).
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in result["error"]
    assert not any(
        call[0] in {"planner", "coder", "patch", "commit"} for call in calls
    )
    assert _chunk_status_value(run_id, 2) == "failed"


# F. Resume checkpoint-skip path must not skip-complete past an incomplete dep.
@pytest.mark.asyncio
async def test_resume_skip_path_blocks_when_dependency_incomplete(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    # Chunk 2 has a valid test checkpoint that would otherwise skip-complete it.
    add_test_checkpoint(run_id, 2)
    calls = []
    patch_resume_git(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in result["error"]
    assert _chunk_status_value(run_id, 2) == "failed"
    assert not any(call[0] == "planner" for call in calls)


# G. Missing dependency reference fails safe (no 500).
@pytest.mark.asyncio
async def test_missing_dependency_reference_fails_safe(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    # Corrupt state: drop chunk 1's row so chunk 2's depends_on=[1] is dangling.
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM chunks WHERE run_id = :run_id AND chunk_number = 1"
        ), {"run_id": run_id})
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in result["error"]
    assert "chunk 1 status: missing" in result["error"]
    assert not any(call[0] in {"planner", "coder"} for call in calls)


# H. Existing multi-chunk sequential success is unchanged.
@pytest.mark.asyncio
async def test_sequential_success_unchanged_with_dependency_guard(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=3)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    status = get_chunk_plan_status(run_id)
    assert [chunk.status for chunk in status.chunks] == [
        "completed",
        "completed",
        "completed",
    ]


# --------------------------------------------------------------------------- #
# Human-triggered patch retry execution (#26D2)
# --------------------------------------------------------------------------- #


def _files_expected(chunk_number: int) -> list[str]:
    return [
        f"created_{chunk_number}.py",
        f"modified_{chunk_number}.py",
        f"deleted_{chunk_number}.py",
    ]


def _seed_failed_chunk(
    run_id: str,
    chunk_number: int = 1,
    *,
    failure_type: PatchFailureType = PatchFailureType.PATCH_DOES_NOT_APPLY,
    human_attempts: int = 0,
) -> str:
    """
    Persist a failed chunk with a stored patch_failure report, exactly as the
    orchestrator would after a real failure, and return its failure_report_id.
    """
    report = build_patch_failure_report(
        failure_type,
        technical_details="original failure",
        changed_files_attempted=[f"modified_{chunk_number}.py"],
        allowed_files=_files_expected(chunk_number),
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=chunk_number,
        max_attempts=2,
        failed_step="patch",
    )
    current = record_initial_attempt(
        report,
        failure_report_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        started_at="2026-06-03T00:00:00+00:00",
    )
    for _ in range(human_attempts):
        current = record_retry_attempt(
            current,
            failure_report_id=str(uuid.uuid4()),
            attempt_id=str(uuid.uuid4()),
            started_at="2026-06-03T00:00:00+00:00",
            recovery_mode="human",
            failure_type=failure_type,
            outcome="failed",
        )
    save_chunk_completion_summary(
        run_id,
        chunk_number,
        patch_failure_report_to_completion_summary(current),
    )
    update_chunk_status(run_id, chunk_number, "failed", report.message)
    return current.failure_report_id


def _forbid_execution(monkeypatch):
    """Make every retry execution seam explode, to prove ineligible == no work."""
    def _boom_async(*_a, **_k):
        raise AssertionError("retry must not run coder/apply/test when ineligible")

    async def _boom_coder(*_a, **_k):
        raise AssertionError("retry must not call coder when ineligible")

    monkeypatch.setattr(chunked_orchestrator, "run_coder", _boom_coder)
    monkeypatch.setattr(chunked_orchestrator, "dry_run_changes", _boom_async)
    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", _boom_async)
    monkeypatch.setattr(chunked_orchestrator, "run_tests", _boom_async)


def _forbid_branch_switch(monkeypatch):
    """
    Make every branch-switching git seam explode, to prove the #26D3a branch
    guard is verify-only: a rejected retry must never checkout/create/switch.
    Install AFTER patch_git_preflight so this override wins.
    """
    def _boom(*_a, **_k):
        raise AssertionError(
            "verify-only retry must not checkout/create/switch branches"
        )

    monkeypatch.setattr(
        chunked_orchestrator.local_git, "create_or_checkout_branch", _boom
    )

    real_run_git = chunked_orchestrator.local_git.run_git

    def _guard_run_git(args, repo_path, timeout=30):
        if args and args[0] in {"checkout", "switch"}:
            raise AssertionError(
                f"verify-only retry must not run git {args[0]}"
            )
        return real_run_git(args, repo_path, timeout)

    monkeypatch.setattr(chunked_orchestrator.local_git, "run_git", _guard_run_git)


def patch_retry_pipeline(
    monkeypatch,
    run_id: str,
    *,
    dry_ok: bool = True,
    apply_ok: bool = True,
    tests_ok: bool = True,
    coder_result: CoderHandoff | None = None,
    apply_failure_report=None,
    calls=None,
    save_code_checkpoint: bool = True,
):
    """Fake the coder/dry-run/apply/test seams for a retry, mirroring reality."""
    resolved_coder = coder_result or make_coder_result(run_id, 1)

    async def fake_coder(plan, run_id_arg, chunk_number=0, **kwargs):
        if calls is not None:
            calls.append(("coder", chunk_number))
        if save_code_checkpoint:
            # The real run_coder writes the code checkpoint the approval path
            # later commits from; model that here.
            save_checkpoint(
                run_id=run_id_arg,
                step="code",
                output=resolved_coder.model_dump(),
                handoff_contract=resolved_coder.model_dump(),
                git_hash="pre-patch",
                tests_passed=False,
                step_completed=True,
                chunk_number=chunk_number,
            )
        return resolved_coder

    def fake_dry(code, repo_path):
        if calls is not None:
            calls.append(("dry_run", None))
        if dry_ok:
            return DryRunResult(ok=True)
        return DryRunResult(
            ok=False,
            failed_path="modified_1.py",
            failed_action="edit",
            error_message=(
                "patch_applier.py: edit old_string not found in modified_1.py. "
                "The text to replace must match the file exactly."
            ),
        )

    def fake_apply(code, run_id_arg, chunk_number=0, *, files_expected, repo_path=None):
        if calls is not None:
            calls.append(("patch", chunk_number))
        if apply_ok:
            _worktree_applied["value"] = True
            return PatchApplyOutcome.from_success(make_patch_result(run_id_arg))
        return PatchApplyOutcome.from_failure(apply_failure_report)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        if calls is not None:
            calls.append(("test", chunk_number))
        if not tests_ok:
            # tester.py rolls back on failure, restoring a clean tree.
            reset_worktree_state()
        return make_test_result(run_id_arg, tests_ok)

    # #26D3a: the retry runs against the working tree, so HEAD must be on the run
    # branch. Model that here so the verify-only branch pre-check passes and the
    # execution seams above are reached.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_branch",
        lambda repo: f"pipewright/{run_id[:8]}",
    )
    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)
    monkeypatch.setattr(chunked_orchestrator, "dry_run_changes", fake_dry)
    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", fake_apply)
    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)


# --- Eligibility / ineligible: nothing executes ---------------------------- #


@pytest.mark.asyncio
async def test_retry_stale_failure_report_id_rejected(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, "stale-id")

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID
    assert _chunk_status_value(run_id, 1) == "failed"


@pytest.mark.asyncio
async def test_retry_missing_or_malformed_report_rejected(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    # Chunk failed but the stored summary is not a patch_failure shape.
    update_chunk_status(run_id, 1, "failed", "boom")
    save_chunk_completion_summary(run_id, 1, {"summary": "not a failure report"})
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, "anything")

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == RETRY_INELIGIBLE_MISSING_REPORT


@pytest.mark.asyncio
async def test_retry_disallowed_failure_type_rejected(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.SCOPE_VIOLATION
    )
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE


@pytest.mark.asyncio
async def test_retry_dirty_working_tree_rejected(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: False
    )
    _forbid_execution(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_DIRTY_WORKTREE


@pytest.mark.asyncio
async def test_retry_unmet_dependency_rejected(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    # Chunk 2 depends on chunk 1, which is not completed.
    frid = _seed_failed_chunk(run_id, 2)
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 2, frid)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET


@pytest.mark.asyncio
async def test_retry_cap_exhausted_rejected(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1, human_attempts=2)
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == RETRY_INELIGIBLE_CAP_EXHAUSTED


# --- Success: pauses at approval, never commits ---------------------------- #


@pytest.mark.asyncio
async def test_retry_success_pauses_at_chunk_approval_not_completed(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "awaiting_chunk_approval"
    assert _chunk_status_value(run_id, 1) == "awaiting_chunk_approval"
    # Regenerated, validated, applied, and tested — but NOT committed.
    assert [c[0] for c in calls if c[0] in {"coder", "dry_run", "patch", "test"}] == [
        "coder",
        "dry_run",
        "patch",
        "test",
    ]
    assert not any(c[0] == "commit" for c in calls)


@pytest.mark.asyncio
async def test_retry_success_stores_recovered_patch_review_summary(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch)
    patch_retry_pipeline(monkeypatch, run_id)

    await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    summary = _read_completion_summary(run_id, 1)
    assert summary["kind"] == RECOVERED_PATCH_REVIEW_KIND
    assert summary["kind"] != PATCH_FAILURE_KIND
    # The recovered attempt is appended with human/recovered/passed semantics.
    recovered = summary["attempts"][-1]
    assert recovered["recovery_mode"] == "human"
    assert recovered["outcome"] == "recovered"
    assert recovered["test_outcome"] == "passed"
    # Prior initial attempt is preserved in history.
    assert any(a["recovery_mode"] == "initial" for a in summary["attempts"])


@pytest.mark.asyncio
async def test_retry_success_then_approval_commits_newest_code_checkpoint(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    # An older, stale code checkpoint touching all three files.
    add_code_checkpoint(run_id, 1)

    # The retry regenerates a DISTINCT, narrower patch (only the create).
    regenerated = CoderHandoff(
        run_id=run_id,
        feature_description="regenerated",
        files_changed=[
            FileChange(
                path="created_1.py",
                action="create",
                content="print('regenerated')\n",
                reason="regenerate",
            ),
        ],
        summary="regenerated chunk 1",
        suggested_memory_entries=[],
    )
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, coder_result=regenerated, calls=calls)

    pause = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)
    assert pause["status"] == "awaiting_chunk_approval"

    approve = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)
    # The only chunk is now completed, so approval advances to final approval (#44A).
    assert approve["status"] == "awaiting_final_approval"

    commit_calls = [c for c in calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    # The newest (regenerated) checkpoint's file list is what gets committed.
    assert commit_calls[0][1] == ["created_1.py"]
    assert _chunk_status_value(run_id, 1) == "completed"


# --- Failure paths persist a fresh report + appended human attempt --------- #


@pytest.mark.asyncio
async def test_retry_dry_run_failure_persists_report_and_skips_apply(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, dry_ok=False, calls=calls)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert result["failure_report_id"] != frid
    # Dry-run gates apply/test entirely.
    assert not any(c[0] in {"patch", "test"} for c in calls)
    assert _chunk_status_value(run_id, 1) == "failed"

    summary = _read_completion_summary(run_id, 1)
    assert summary["kind"] == PATCH_FAILURE_KIND
    assert summary["failure_type"] == PatchFailureType.PATCH_DOES_NOT_APPLY.value
    # Initial + appended human retry attempt.
    modes = [a["recovery_mode"] for a in summary["attempts"]]
    assert modes == ["initial", "human"]


@pytest.mark.asyncio
async def test_retry_apply_failure_persists_report_and_appends_attempt(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    apply_failure = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details="hunk failed",
        changed_files_attempted=["modified_1.py"],
        allowed_files=_files_expected(1),
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="patch",
    )
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(
        monkeypatch,
        run_id,
        apply_ok=False,
        apply_failure_report=apply_failure,
        calls=calls,
    )

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert result["failure_report_id"] != frid
    assert not any(c[0] == "test" for c in calls)
    assert not any(c[0] == "commit" for c in calls)
    summary = _read_completion_summary(run_id, 1)
    assert summary["kind"] == PATCH_FAILURE_KIND
    modes = [a["recovery_mode"] for a in summary["attempts"]]
    assert modes == ["initial", "human"]


@pytest.mark.asyncio
async def test_retry_test_failure_persists_report_with_rollback(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=False, calls=calls)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert not any(c[0] == "commit" for c in calls)
    summary = _read_completion_summary(run_id, 1)
    assert summary["failure_type"] == (
        PatchFailureType.TEST_FAILURE_AFTER_APPLY.value
    )
    assert summary["rollback_performed"] is True
    recovered = summary["attempts"][-1]
    assert recovered["recovery_mode"] == "human"
    assert recovered["test_outcome"] == "failed"


# --- Safety invariants ----------------------------------------------------- #


@pytest.mark.asyncio
async def test_retry_does_not_mutate_files_expected(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    before = get_chunk_plan_status(run_id).chunks[0].files_expected
    patch_git_preflight(monkeypatch)
    patch_retry_pipeline(monkeypatch, run_id)

    await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    after = get_chunk_plan_status(run_id).chunks[0].files_expected
    assert after == before == _files_expected(1)


@pytest.mark.asyncio
async def test_retry_out_of_scope_regenerated_patch_blocked(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    out_of_scope = CoderHandoff(
        run_id=run_id,
        feature_description="drift",
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
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, coder_result=out_of_scope, calls=calls)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    # Scope guard fires before dry-run/apply/test and before any commit.
    assert not any(c[0] in {"dry_run", "patch", "test", "commit"} for c in calls)
    summary = _read_completion_summary(run_id, 1)
    assert summary["failure_type"] == PatchFailureType.SCOPE_VIOLATION.value
    assert _chunk_status_value(run_id, 1) == "failed"


@pytest.mark.asyncio
async def test_recovered_awaiting_chunk_does_not_unblock_dependent(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    pause = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)
    assert pause["status"] == "awaiting_chunk_approval"

    # Chunk 1 is recovered-but-uncommitted (awaiting approval). Chunk 2 must not
    # be allowed to run: its dependency is not completed.
    result = await chunked_orchestrator.execute_approved_chunks(run_id)
    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert "DEPENDENCY_NOT_MET" in result["error"]


@pytest.mark.asyncio
async def test_retry_does_not_write_memory(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch)
    patch_retry_pipeline(monkeypatch, run_id)

    memory_calls = {"count": 0}

    def spy_mark_stale(*_a, **_k):
        memory_calls["count"] += 1

    monkeypatch.setattr(chunked_orchestrator, "mark_fact_stale", spy_mark_stale)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "awaiting_chunk_approval"
    assert memory_calls["count"] == 0


@pytest.mark.asyncio
async def test_resume_does_not_skip_complete_recovered_but_uncommitted_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    # Crash window: a retry produced a passing test checkpoint but the process
    # died before the approval pause, so NO chunk commit exists. Resume must NOT
    # skip-complete the chunk; the existing _verify_completed_checkpoint_safe
    # requires the commit.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    update_chunk_status(run_id, 1, "running")  # mid-retry crash state
    calls = []
    patch_resume_git(monkeypatch, calls)
    # No chunk commit exists.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_message_exists",
        lambda repo, prefix: False,
    )
    patch_success_pipeline(monkeypatch, run_id, calls)

    with pytest.raises(RuntimeError, match="unsafe resume recovery"):
        await chunked_orchestrator.resume_chunked_pipeline(run_id)

    assert _chunk_status_value(run_id, 1) != "completed"


@pytest.mark.asyncio
async def test_retry_unexpected_coder_error_marks_chunk_failed(
    monkeypatch, tmp_repo, tracked_runs
):
    # A hard raise from the coder/test seam must not leave the chunk stuck in
    # "running"; the execution guard turns it into a failed chunk.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)

    async def boom_coder(*_a, **_k):
        raise RuntimeError("coder LLM exploded")

    monkeypatch.setattr(chunked_orchestrator, "run_coder", boom_coder)
    monkeypatch.setattr(
        chunked_orchestrator,
        "apply_patch_guarded",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("apply must not run after a coder error")
        ),
    )

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert result["failed_chunk"] == 1
    assert "coder LLM exploded" in result["error"]
    assert _chunk_status_value(run_id, 1) == "failed"


@pytest.mark.asyncio
async def test_retry_reevaluates_eligibility_inside_lock_not_pre_lock_snapshot(
    monkeypatch, tmp_repo, tracked_runs
):
    # TOCTOU guard (#26D2): a concurrent retry that wins the lock first replaces
    # the stored failure report (new failure_report_id) while this call is blocked
    # on the lock. Eligibility must be decided from that fresh in-lock state, not
    # the pre-lock snapshot — otherwise a double-submit could re-run an already
    # consumed failure_report_id and bypass MAX_HUMAN_RETRIES.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    old_frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)
    # If any eligibility-relevant read used the pre-lock snapshot, the request's
    # still-matching old_frid would pass and execution would fire and explode.
    _forbid_execution(monkeypatch)

    real_lock = chunked_orchestrator.project_repo_lock

    @asynccontextmanager
    async def mutating_lock(project_id):
        async with real_lock(project_id):
            # Model the winning concurrent retry: the stored report now carries a
            # different failure_report_id, making old_frid stale.
            _seed_failed_chunk(run_id, 1)
            yield

    monkeypatch.setattr(chunked_orchestrator, "project_repo_lock", mutating_lock)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, old_frid)

    # In-lock re-read sees the new frid -> stale -> clean 409, no execution.
    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID
    assert _chunk_status_value(run_id, 1) == "failed"


# --- #26D3a: verify-only branch pre-check ---------------------------------- #


@pytest.mark.asyncio
async def test_retry_wrong_branch_rejected_without_side_effects(
    monkeypatch, tmp_repo, tracked_runs
):
    # Branch guard (#26D3a): if the target repo's HEAD is not on the run branch,
    # retry must reject with a side-effect-free 409 — no checkout, no execution,
    # no chunk-status/summary mutation. Verify-only: the user moves the branch.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch)
    # HEAD is on some other branch, not pipewright/<run_id[:8]>.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_branch",
        lambda repo: "feature/something-else",
    )
    # Differential: without the pre-check this falls through to execution and the
    # forbidden seams below explode instead of returning a clean rejection.
    _forbid_execution(monkeypatch)
    _forbid_branch_switch(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "retry_ineligible"
    assert result["eligible"] is False
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_WRONG_BRANCH
    # The detail names both the current and the expected run branch.
    assert "feature/something-else" in result["detail"]
    assert f"pipewright/{run_id[:8]}" in result["detail"]
    # Side-effect-free: chunk still failed and the stored report is the ORIGINAL
    # patch_failure (not a recovered/running mutation).
    assert _chunk_status_value(run_id, 1) == "failed"
    summary = _read_completion_summary(run_id, 1)
    assert summary["kind"] == PATCH_FAILURE_KIND


@pytest.mark.asyncio
async def test_retry_undeterminable_branch_rejected(
    monkeypatch, tmp_repo, tracked_runs
):
    # If the current branch cannot be determined (detached HEAD / git error),
    # retry must not guess — it rejects with the same 409 wrong-branch reason.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch)

    def _detached(repo):
        raise RuntimeError("[GIT] current branch is empty")

    monkeypatch.setattr(
        chunked_orchestrator.local_git, "get_current_branch", _detached
    )
    _forbid_execution(monkeypatch)
    _forbid_branch_switch(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_WRONG_BRANCH
    assert f"pipewright/{run_id[:8]}" in result["detail"]
    assert _chunk_status_value(run_id, 1) == "failed"


@pytest.mark.asyncio
async def test_retry_on_expected_branch_proceeds_to_execution(
    monkeypatch, tmp_repo, tracked_runs
):
    # Happy path: when HEAD is already on the run branch, the read-only branch
    # guard passes and retry proceeds to the existing #26D2 execution behavior.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    seen = {}

    def _record_branch(repo):
        seen["branch"] = f"pipewright/{run_id[:8]}"
        return seen["branch"]

    # Override after patch_retry_pipeline so we can prove the guard read the branch.
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "get_current_branch", _record_branch
    )

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert seen["branch"] == f"pipewright/{run_id[:8]}"
    assert result["status"] == "awaiting_chunk_approval"
    assert _chunk_status_value(run_id, 1) == "awaiting_chunk_approval"


@pytest.mark.asyncio
async def test_retry_rejection_never_switches_branch(
    monkeypatch, tmp_repo, tracked_runs
):
    # Verify-only regression guard (#26D3a): even when HEAD is correctly on the
    # run branch, a rejected retry (here: a stale failure_report_id) must perform
    # no branch checkout/switch. If a future change auto-checks-out, the
    # _forbid_branch_switch booms fire and this test fails.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)
    _forbid_branch_switch(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, "stale-id")

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID
    assert _chunk_status_value(run_id, 1) == "failed"


# --------------------------------------------------------------------------
# #28D — display-only runtime test verdict is persisted after the chunk's
# test run, without ever changing pass/fail or run outcome.
# --------------------------------------------------------------------------


def _create_run_with_command(tmp_repo, tracked_runs, test_command: str):
    project = create_project(
        name=f"Verdict Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command=test_command,
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Execute chunks",
        make_triage(run_id, project["id"], 1),
    )
    approve_chunk_plan(run_id)
    return run_id, project


def _override_tests(monkeypatch, result: PipelineTestResult):
    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda patch, run_id, chunk_number=0: result,
    )


@pytest.mark.asyncio
async def test_weak_command_persists_weak_verdict_but_chunk_completes(
    monkeypatch, tmp_repo, tracked_runs
):
    # Default project test command is "python --version" (weak). The chunk must
    # still COMPLETE (pass/fail is exit-code based), and the verdict recorded weak.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert get_chunk_plan_status(run_id).chunks[0].status == "completed"
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "weak"  # display-only, did not block completion
    assert stored["command_quality"] == "weak"


@pytest.mark.asyncio
async def test_passing_pytest_persists_strong_verdict(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = _create_run_with_command(tmp_repo, tracked_runs, "pytest")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    _override_tests(monkeypatch, PipelineTestResult(
        run_id=run_id,
        passed=True,
        output="===== 5 passed in 0.10s =====",
        total_tests=5,
        passed_tests=5,
        failed_tests=0,
    ))

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "strong"
    assert stored["passed_tests"] == 5
    assert stored["total_tests"] == 5
    assert stored["counts_parsed"] is True
    assert stored["zero_tests_detected"] is False


@pytest.mark.asyncio
async def test_zero_tests_persists_weak_but_does_not_fail_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    # A recognized runner that collected 0 items, exit 0. This slice must NOT
    # treat zero tests as a failure: the chunk completes and the verdict is weak.
    run_id, _project = _create_run_with_command(tmp_repo, tracked_runs, "pytest")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    _override_tests(monkeypatch, PipelineTestResult(
        run_id=run_id,
        passed=True,  # exit-code based success
        output="collected 0 items\n\nno tests ran in 0.01s",
        total_tests=0,
        passed_tests=0,
        failed_tests=0,
    ))

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert get_chunk_plan_status(run_id).chunks[0].status == "completed"
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "weak"
    assert stored["zero_tests_detected"] is True


@pytest.mark.asyncio
async def test_test_failure_records_verdict_and_fails_by_exit_code(
    monkeypatch, tmp_repo, tracked_runs
):
    # A failing test run fails the chunk via the exit code (existing behavior),
    # NOT via the verdict. The verdict is still recorded as evidence (unknown:
    # non-zero exit on a recognized runner is never strong).
    run_id, _project = _create_run_with_command(tmp_repo, tracked_runs, "pytest")
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    _override_tests(monkeypatch, PipelineTestResult(
        run_id=run_id,
        passed=False,
        output="1 failed, 4 passed in 1.0s",
        total_tests=5,
        passed_tests=4,
        failed_tests=1,
    ))

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert get_chunk_plan_status(run_id).chunks[0].status == "failed"
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "unknown"
    assert stored["verdict"] != "strong"
    assert stored["passed_tests"] == 4
    assert stored["failed_tests"] == 1


# --------------------------------------------------------------------------
# #28 bug: every run_tests path must persist the runtime verdict before the
# pass/fail branch. The auto/no-human-review path (above) already did; the
# #26D2 human retry path (_execute_retry_attempt) ran tests WITHOUT persisting,
# so a retried-then-completed chunk carried a NULL verdict and silently slipped
# past the #28F final-approval acknowledgement gate. These lock that path in.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_path_persists_weak_verdict_for_version_probe(
    monkeypatch, tmp_repo, tracked_runs
):
    # A low-risk chunk that failed once and is retried by a human. The project
    # test command is "python --version" (weak). The retry succeeds and pauses at
    # the chunk approval gate; the weak verdict MUST be persisted at test time
    # (before the fix it was NULL here).
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "awaiting_chunk_approval"
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored is not None  # was None before the fix
    assert stored["verdict"] == "weak"
    assert stored["command_quality"] == "weak"


@pytest.mark.asyncio
async def test_retry_path_persists_verdict_before_test_failure_branch(
    monkeypatch, tmp_repo, tracked_runs
):
    # The retry's own test run fails. The verdict is still recorded (evidence)
    # before the failure branch returns; pass/fail stays exit-code based, so the
    # chunk re-fails.
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    patch_git_preflight(monkeypatch, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=False)

    await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    # A weak command that also exited non-zero stays weak (never strong).
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored is not None
    assert stored["verdict"] == "weak"
    assert stored["verdict"] != "strong"


# --------------------------------------------------------------------------
# #28 end-to-end: the low-risk / requires_human_review=false auto path persists
# the verdict, and that persisted verdict is what the #28F final-approval gate
# enforces. Proves the reported symptom (PR/approval slipping past on a weak
# command) is closed end to end.
# --------------------------------------------------------------------------


def _seed_test_checkpoint(run_id, chunk_number, git_hash):
    # The chunk's latest "test" checkpoint git hash is the canonical diff identity
    # the acknowledgement is bound to. The mocked pipeline does not write one, so
    # seed it exactly as the real tester would.
    save_checkpoint(
        run_id=run_id,
        step="test",
        output={"passed": True},
        handoff_contract={"passed": True},
        git_hash=git_hash,
        tests_passed=True,
        chunk_number=chunk_number,
    )


@pytest.mark.asyncio
async def test_low_risk_weak_chunk_blocks_final_approval_until_acknowledged(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)  # "python --version"
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)
    assert result["status"] == "awaiting_final_approval"

    # The auto-complete path persisted the weak verdict on the completed chunk.
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "weak"

    client = TestClient(app)
    # Final approval is blocked until the weak verdict is acknowledged.
    blocked = client.post(f"/runs/{run_id}/final-approval/approve")
    assert blocked.status_code == 409

    _seed_test_checkpoint(run_id, 1, "HASH_A")
    ack = client.post(
        f"/runs/{run_id}/chunks/1/test-validation/acknowledge",
        json={"reason": "manually verified"},
    )
    assert ack.status_code == 200

    approved = client.post(f"/runs/{run_id}/final-approval/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "final_approved"


@pytest.mark.asyncio
async def test_low_risk_strong_chunk_final_approval_needs_no_acknowledgement(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = _create_run_with_command(tmp_repo, tracked_runs, "pytest")
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)
    _override_tests(monkeypatch, PipelineTestResult(
        run_id=run_id,
        passed=True,
        output="===== 5 passed in 0.10s =====",
        total_tests=5,
        passed_tests=5,
        failed_tests=0,
    ))

    result = await chunked_orchestrator.execute_approved_chunks(run_id)
    assert result["status"] == "awaiting_final_approval"
    assert get_chunk_test_run_verdict(run_id, 1)["verdict"] == "strong"

    client = TestClient(app)
    approved = client.post(f"/runs/{run_id}/final-approval/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "final_approved"
