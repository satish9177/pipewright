"""
test_chunked_orchestrator_reviewer.py
Orchestrator-level regression tests for advisory reviewer placement.

These prove the CRITICAL invariant: the chunk outcome is identical whether the
reviewer is invoked, succeeds, or raises — and that the reviewer runs ONLY on a
standing applied diff with passing tests (never on patch-apply failure or a
rolled-back test failure). They reuse the existing chunked_orchestrator fake
harness; no real LLM, git, or GitHub.
"""

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.pipeline import chunked_orchestrator
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.patch_applier import PatchApplyOutcome
from backend.pipeline.patch_failures import PatchFailureType, build_patch_failure_report
from backend.tests.test_chunked_orchestrator import (
    _chunk_status_value,
    _files_expected,
    _seed_failed_chunk,
    create_run,
    make_test_result,
    patch_git_preflight,
    patch_retry_pipeline,
    patch_success_pipeline,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in ("chunk_reviews", "approval_gates", "checkpoints", "chunks"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :r"), {"r": run_id}
                )
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})


def _raising_review():
    async def boom(**kwargs):
        raise RuntimeError("reviewer exploded")
    return boom


@pytest.mark.asyncio
async def test_reviewer_invoked_after_successful_tests_with_diff(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    seen = {}

    async def fake_review(*, run_id, project_id, chunk, code, patch, test_result):
        seen["called"] = True
        seen["chunk_number"] = chunk.chunk_number
        seen["passed"] = test_result.passed
        seen["has_diff"] = bool(getattr(patch, "diff", None))
        return None

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", fake_review)

    await chunked_orchestrator.execute_approved_chunks(run_id)

    # Invoked once tests passed, with a standing applied diff, before commit.
    assert seen.get("called") is True
    assert seen["chunk_number"] == 1
    assert seen["passed"] is True
    assert seen["has_diff"] is True
    assert any(call[0] == "commit" for call in calls)


@pytest.mark.asyncio
async def test_reviewer_failure_does_not_change_commit_outcome(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", _raising_review())

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    # Identical to the no-reviewer success path: committed + awaiting final approval.
    assert any(call[0] == "commit" for call in calls)
    assert result["status"] == "awaiting_final_approval"
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"


@pytest.mark.asyncio
async def test_reviewer_failure_does_not_change_high_risk_pause_outcome(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs, review_chunks={1})
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", _raising_review())

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    # Identical to the no-reviewer high-risk path: paused for approval, no commit.
    assert result["status"] == "awaiting_chunk_approval"
    assert result["chunk_number"] == 1
    assert not any(call[0] == "commit" for call in calls)
    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "awaiting_chunk_approval"


@pytest.mark.asyncio
async def test_reviewer_not_run_on_test_failure(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    # Override the success pipeline's passing tester with a failing one.
    monkeypatch.setattr(
        chunked_orchestrator, "run_tests",
        lambda *a, **k: make_test_result(run_id, passed=False),
    )

    called = {"value": False}

    async def rec(**kwargs):
        called["value"] = True

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", rec)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert called["value"] is False
    assert not any(call[0] == "commit" for call in calls)


@pytest.mark.asyncio
async def test_reviewer_not_run_on_patch_apply_failure(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details="hunk failed",
        changed_files_attempted=["created_1.py"],
        allowed_files=["created_1.py"],
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="patch",
    )

    def fake_fail(code, run_id, chunk_number=0, *, files_expected, repo_path=None):
        return PatchApplyOutcome.from_failure(report)

    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", fake_fail)
    monkeypatch.setattr(
        chunked_orchestrator, "run_tests",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_tests must not run after a failed apply")
        ),
    )

    called = {"value": False}

    async def rec(**kwargs):
        called["value"] = True

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", rec)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert called["value"] is False


# --- Retry path (#26) -------------------------------------------------------- #
# The advisory reviewer must also run on the successful human-triggered retry
# path, with the same best-effort/swallow invariant as the normal path.


@pytest.mark.asyncio
async def test_reviewer_invoked_on_successful_retry(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    seen = {}

    async def fake_review(*, run_id, project_id, chunk, code, patch, test_result):
        seen["called"] = True
        seen["chunk_number"] = chunk.chunk_number
        seen["passed"] = test_result.passed
        seen["has_diff"] = bool(getattr(patch, "diff", None))
        return None

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", fake_review)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    # Invoked after the retry patch applied and tests passed, before the pause.
    assert seen.get("called") is True
    assert seen["chunk_number"] == 1
    assert seen["passed"] is True
    assert seen["has_diff"] is True
    # Retry outcome unchanged: still pauses at chunk approval, never commits.
    assert result["status"] == "awaiting_chunk_approval"
    assert _chunk_status_value(run_id, 1) == "awaiting_chunk_approval"
    assert not any(call[0] == "commit" for call in calls)


@pytest.mark.asyncio
async def test_reviewer_failure_does_not_change_retry_outcome(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", _raising_review())

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    # Identical to the no-reviewer retry-success path: paused for approval,
    # never committed, despite the reviewer raising.
    assert result["status"] == "awaiting_chunk_approval"
    assert _chunk_status_value(run_id, 1) == "awaiting_chunk_approval"
    assert not any(call[0] == "commit" for call in calls)


@pytest.mark.asyncio
async def test_reviewer_not_run_on_retry_test_failure(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(run_id, 1)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=False, calls=calls)

    called = {"value": False}

    async def rec(**kwargs):
        called["value"] = True

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", rec)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert called["value"] is False
    assert not any(call[0] == "commit" for call in calls)


@pytest.mark.asyncio
async def test_reviewer_not_run_on_retry_apply_failure(monkeypatch, tmp_repo, tracked_runs):
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

    called = {"value": False}

    async def rec(**kwargs):
        called["value"] = True

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", rec)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert called["value"] is False
    # No tests ran (apply failed before testing), so the reviewer is unreachable.
    assert not any(call[0] == "test" for call in calls)
