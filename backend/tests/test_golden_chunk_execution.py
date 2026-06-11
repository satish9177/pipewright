"""
test_golden_chunk_execution.py
Phase 2 item 10 — golden no-behavior-change snapshots (impl brief §10.3).

End-to-end `execute_approved_chunks` behavior captured from the PRE-refactor
code (develop @ "phase 1 done"), as full observable snapshots: the returned
result, run status/step, chunk status/error, the FULL normalized
completion_summary stored in chunks, approval gates, commit invocations, and
per-stage invocation counts.

These are the parity baseline for the Phase 2 strangler refactor (items
10-12): item 10 must keep them green untouched; item 11 runs the same
scenarios through the driver. If one of these fails after a refactor, the
refactor changed observable behavior — fix the code, never the snapshot.

Scenarios (§10.3): fresh success; TEST_REGRESSION; HARNESS_ERROR + auto-retry
(recovered, and budget-exhausted); SCOPE_VIOLATION; DIRTY_WORKTREE.
Reuses the existing chunked_orchestrator fake harness; no real AI, git
mutation, or GitHub.
"""

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline import chunked_orchestrator
from backend.pipeline.patch_applier import PatchApplyOutcome
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
    default_message_for_failure_type,
)
from backend.pipeline.scope_expansion_store import (
    list_scope_expansion_requests_for_chunk,
)
from backend.tests.test_chunked_orchestrator import (
    _read_completion_summary,
    create_run,
    make_failed_test_result,
    make_test_result,
    patch_git_preflight,
    patch_success_pipeline,
    reset_worktree_state,
)

pytestmark = pytest.mark.unit

ID_SENTINEL = "<id>"
TS_SENTINEL = "<timestamp>"
FILES = ["created_1.py", "modified_1.py", "deleted_1.py"]


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "scope_expansion_requests",
                "chunk_attempts",
                "chunk_reviews",
                "approval_gates",
                "checkpoints",
                "chunks",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :r"), {"r": run_id}
                )
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})


def _normalized_summary(run_id: str, chunk_number: int = 1) -> dict:
    """Stored completion_summary with volatile ids/timestamps replaced."""
    summary = _read_completion_summary(run_id, chunk_number)
    if summary.get("failure_report_id"):
        summary["failure_report_id"] = ID_SENTINEL
    for attempt in summary.get("attempts", []):
        attempt["attempt_id"] = ID_SENTINEL
        attempt["started_at"] = TS_SENTINEL
    return summary


def _run_row(run_id: str) -> tuple:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status, current_step FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()


def _chunk_row(run_id: str, chunk_number: int = 1) -> tuple:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status, error_message FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()


def _gate_counts(run_id: str) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT approval_type, COUNT(*) FROM approval_gates
            WHERE run_id = :run_id GROUP BY approval_type
        """), {"run_id": run_id}).fetchall()
    return {row[0]: row[1] for row in rows}


def _stage_counts(calls: list) -> dict[str, int]:
    names = [c[0] for c in calls]
    return {
        stage: names.count(stage)
        for stage in ("baseline", "planner", "coder", "dry_run", "patch", "test",
                      "review", "commit")
    }


def _attempt(
    *,
    attempt_number: int,
    recovery_mode: str,
    failure_type: str | None,
    failed_step: str | None,
    changed_files_attempted: list[str],
    changed_files_actual: list[str],
    scope_ok: bool,
    test_outcome: str,
    outcome: str,
    working_tree_clean: bool,
    rollback_performed: bool,
) -> dict:
    """One expected PatchRecoveryAttempt as stored (json mode), normalized."""
    return {
        "attempt_id": ID_SENTINEL,
        "attempt_number": attempt_number,
        "started_at": TS_SENTINEL,
        "recovery_mode": recovery_mode,
        "failure_type": failure_type,
        "failed_step": failed_step,
        "changed_files_attempted": changed_files_attempted,
        "changed_files_actual": changed_files_actual,
        "scope_ok": scope_ok,
        "preimage_matched": None,
        "model_used": None,
        "test_outcome": test_outcome,
        "outcome": outcome,
        "human_decision": None,
        "working_tree_clean": working_tree_clean,
        "rollback_performed": rollback_performed,
    }


def _success_summary(attempts: list[dict] | None = None) -> dict:
    expected = {
        "chunk_title": "Chunk 1",
        "chunk_description": "Do chunk 1",
        "files_created": ["created_1.py"],
        "files_modified": ["modified_1.py"],
        "files_deleted": ["deleted_1.py"],
        "key_decisions": ["Plan step", "Code step"],
        "tests_added_or_updated": [],
        "tests_added": [],
        "summary": "Goal: Implement the chunk.\nCoder summary: Chunk 1 coded",
        "suggested_memory_entries": ["remember chunk"],
    }
    if attempts:
        expected["attempts"] = attempts
    return expected


def _failure_summary(
    failure_type: PatchFailureType,
    *,
    technical_details: str | None,
    changed_files_attempted: list[str],
    suggested_actions: list[str],
    rollback_performed: bool,
    working_tree_clean: bool,
    retry: dict,
    failed_step: str,
    manual_intervention_needed: bool,
    attempts: list[dict],
) -> dict:
    return {
        "kind": "patch_failure",
        "failure_type": failure_type.value,
        "message": default_message_for_failure_type(failure_type),
        "technical_details": technical_details,
        "changed_files_attempted": changed_files_attempted,
        "changed_files_actual": [],
        "allowed_files": FILES,
        "suggested_actions": suggested_actions,
        "rollback_performed": rollback_performed,
        "working_tree_clean": working_tree_clean,
        "retry": retry,
        "stale_index_hint": False,
        "chunk_number": 1,
        "failed_step": failed_step,
        "manual_intervention_needed": manual_intervention_needed,
        "failure_report_id": ID_SENTINEL,
        "attempts": attempts,
    }


@pytest.mark.asyncio
async def test_golden_fresh_success(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert result["completed_chunks"] == 1
    assert result["final_approval_required"] is True
    assert result["branch_name"] == f"pipewright/{run_id[:8]}"
    assert _run_row(run_id)[0] == "awaiting_final_approval"
    assert _chunk_row(run_id) == ("completed", None)
    assert _gate_counts(run_id) == {"final": 1}
    commits = [c for c in calls if c[0] == "commit"]
    assert commits == [("commit", FILES, "chunk 1: Chunk 1", project["repo_path"])]
    assert _stage_counts(calls) == {
        "baseline": 1, "planner": 1, "coder": 1, "dry_run": 1, "patch": 1,
        "test": 1, "review": 1, "commit": 1,
    }
    assert _normalized_summary(run_id) == _success_summary()


@pytest.mark.asyncio
async def test_golden_test_regression(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        reset_worktree_state()  # tester rolled back; tree is clean again
        return make_failed_test_result(
            run_id_arg, "1 failed, 4 passed in 1.0s", exit_code=1
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    expected_message = default_message_for_failure_type(
        PatchFailureType.TEST_REGRESSION
    )
    assert result == {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": 1,
        "error": expected_message,
    }
    assert _run_row(run_id) == ("failed", "chunk_1_failed")
    assert _chunk_row(run_id) == ("failed", expected_message)
    assert _gate_counts(run_id) == {}
    assert _stage_counts(calls) == {
        "baseline": 1, "planner": 1, "coder": 1, "dry_run": 1, "patch": 1,
        "test": 1, "review": 0, "commit": 0,
    }
    assert _normalized_summary(run_id) == _failure_summary(
        PatchFailureType.TEST_REGRESSION,
        technical_details="exit_code=1; timed_out=False\n1 failed, 4 passed in 1.0s",
        changed_files_attempted=FILES,
        # Deliberate item-13 change (proposal §4.2 "un-dead-ending
        # TEST_REGRESSION (steerable)"): a regression now advertises
        # retry_with_instruction, which finally has an execution path. Plain
        # retry stays excluded; everything else in this golden is unchanged.
        suggested_actions=[
            "retry_with_instruction", "reject_chunk",
            "mark_manual_intervention", "view_details",
        ],
        rollback_performed=True,
        working_tree_clean=True,
        retry={"attempts": 0, "max_attempts": 1, "retryable": False},
        failed_step="test",
        manual_intervention_needed=False,
        attempts=[
            _attempt(
                attempt_number=1,
                recovery_mode="initial",
                failure_type="TEST_REGRESSION",
                failed_step="test",
                changed_files_attempted=FILES,
                changed_files_actual=[],
                scope_ok=True,
                test_outcome="failed",
                outcome="failed",
                working_tree_clean=True,
                rollback_performed=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_golden_harness_error_auto_retry_recovers(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    results = [
        make_failed_test_result(
            run_id, "Fatal Python error: init_sys_streams", exit_code=1
        ),
        make_test_result(run_id, True),
    ]

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        result = results.pop(0)
        if not result.passed:
            reset_worktree_state()  # tester rolled back; tree is clean again
        return result

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert result["completed_chunks"] == 1
    assert _run_row(run_id)[0] == "awaiting_final_approval"
    assert _chunk_row(run_id) == ("completed", None)
    assert _gate_counts(run_id) == {"final": 1}
    commits = [c for c in calls if c[0] == "commit"]
    assert commits == [("commit", FILES, "chunk 1: Chunk 1", project["repo_path"])]
    # Auto-retry re-runs code -> preflight -> apply -> test exactly once more.
    assert _stage_counts(calls) == {
        "baseline": 1, "planner": 1, "coder": 2, "dry_run": 2, "patch": 2,
        "test": 2, "review": 1, "commit": 1,
    }
    assert _normalized_summary(run_id) == _success_summary(attempts=[
        _attempt(
            attempt_number=1,
            recovery_mode="initial",
            failure_type="HARNESS_ERROR",
            failed_step="test",
            changed_files_attempted=FILES,
            changed_files_actual=[],
            scope_ok=True,
            test_outcome="failed",
            outcome="failed",
            working_tree_clean=True,
            rollback_performed=True,
        ),
        _attempt(
            attempt_number=2,
            recovery_mode="auto",
            failure_type=None,
            failed_step=None,
            changed_files_attempted=FILES,
            changed_files_actual=FILES,
            scope_ok=True,
            test_outcome="passed",
            outcome="recovered",
            working_tree_clean=False,
            rollback_performed=False,
        ),
    ])


@pytest.mark.asyncio
async def test_golden_harness_error_auto_retry_exhausted(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        reset_worktree_state()  # tester rolled back; tree is clean again
        return make_failed_test_result(
            run_id_arg, "Fatal Python error: init_sys_streams", exit_code=1
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    expected_message = default_message_for_failure_type(
        PatchFailureType.HARNESS_ERROR
    )
    assert result == {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": 1,
        "error": expected_message,
    }
    assert _run_row(run_id) == ("failed", "chunk_1_failed")
    assert _chunk_row(run_id) == ("failed", expected_message)
    assert _gate_counts(run_id) == {}
    assert _stage_counts(calls) == {
        "baseline": 1, "planner": 1, "coder": 2, "dry_run": 2, "patch": 2,
        "test": 2, "review": 0, "commit": 0,
    }
    assert _normalized_summary(run_id) == _failure_summary(
        PatchFailureType.HARNESS_ERROR,
        technical_details=(
            "exit_code=1; timed_out=False\nFatal Python error: init_sys_streams"
        ),
        changed_files_attempted=FILES,
        suggested_actions=[
            "retry_with_instruction", "reject_chunk",
            "mark_manual_intervention", "view_details",
        ],
        rollback_performed=True,
        working_tree_clean=True,
        retry={"attempts": 1, "max_attempts": 1, "retryable": False},
        failed_step="test",
        manual_intervention_needed=True,
        attempts=[
            _attempt(
                attempt_number=1,
                recovery_mode="initial",
                failure_type="HARNESS_ERROR",
                failed_step="test",
                changed_files_attempted=FILES,
                changed_files_actual=[],
                scope_ok=True,
                test_outcome="failed",
                outcome="failed",
                working_tree_clean=True,
                rollback_performed=True,
            ),
            _attempt(
                attempt_number=2,
                recovery_mode="auto",
                failure_type="HARNESS_ERROR",
                failed_step="test",
                changed_files_attempted=FILES,
                changed_files_actual=[],
                scope_ok=True,
                test_outcome="failed",
                outcome="manual_intervention",
                working_tree_clean=True,
                rollback_performed=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_golden_scope_violation(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    out_of_scope = CoderHandoff(
        run_id=run_id,
        feature_description="enriched",
        files_changed=[
            FileChange(
                path="outside.py",
                action="create",
                content="print('no')\n",
                reason="out of scope",
            )
        ],
        summary="bad scope",
        suggested_memory_entries=[],
    )

    async def fake_coder(plan, run_id_arg, chunk_number=0, **kwargs):
        calls.append(("coder", chunk_number))
        return out_of_scope

    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    expected_message = default_message_for_failure_type(
        PatchFailureType.SCOPE_VIOLATION
    )
    assert result == {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": 1,
        "error": expected_message,
    }
    # A clean SCOPE_VIOLATION surfaces a pending scope-expansion request and
    # moves the RUN (not the chunk) to awaiting_scope_approval (#27).
    assert _run_row(run_id) == (
        "awaiting_scope_approval", "chunk_1_awaiting_scope_approval",
    )
    assert _chunk_row(run_id) == ("failed", expected_message)
    assert _gate_counts(run_id) == {}
    requests = list_scope_expansion_requests_for_chunk(run_id, 1)
    assert [request.status for request in requests] == ["pending"]
    assert _stage_counts(calls) == {
        "baseline": 1, "planner": 1, "coder": 1, "dry_run": 0, "patch": 0,
        "test": 0, "review": 0, "commit": 0,
    }
    assert _normalized_summary(run_id) == _failure_summary(
        PatchFailureType.SCOPE_VIOLATION,
        technical_details=(
            "scope_guard.py: coder wrote file outside approved scope. "
            "path=outside.py | "
            "files_expected=['created_1.py', 'deleted_1.py', 'modified_1.py']"
        ),
        changed_files_attempted=["outside.py"],
        suggested_actions=[
            "retry_with_instruction", "reject_chunk",
            "mark_manual_intervention", "view_details",
        ],
        rollback_performed=False,
        working_tree_clean=True,
        retry={"attempts": 0, "max_attempts": 0, "retryable": False},
        failed_step="patch",
        manual_intervention_needed=False,
        attempts=[
            _attempt(
                attempt_number=1,
                recovery_mode="initial",
                failure_type="SCOPE_VIOLATION",
                failed_step="patch",
                changed_files_attempted=["outside.py"],
                changed_files_actual=[],
                scope_ok=False,
                test_outcome="not_run",
                outcome="failed",
                working_tree_clean=True,
                rollback_performed=False,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_golden_apply_phase_unknown_failure_never_auto_retries(
    monkeypatch, tmp_repo, tracked_runs
):
    """
    Impl brief §11.2a landmine #1: UNKNOWN_PATCH_FAILURE maps to the
    INFRA_ERROR outcome class, but an apply-phase occurrence is NEVER
    auto-retried today — auto-retry fires only in the verify/test branch,
    gated on HARNESS_ERROR + non-TIMEOUT integrity. The driver must keep the
    today-equivalent gate, not trigger off the class. Captured pre-refactor.
    """
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    unknown = build_patch_failure_report(
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
        technical_details="patch_applier.py: unexpected apply error",
        changed_files_attempted=FILES,
        allowed_files=FILES,
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="patch",
    )

    def fake_unknown_apply(
        code, run_id_arg, chunk_number=0, *, files_expected, repo_path=None
    ):
        calls.append(("patch", chunk_number))
        return PatchApplyOutcome.from_failure(unknown)

    monkeypatch.setattr(
        chunked_orchestrator, "apply_patch_guarded", fake_unknown_apply
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    expected_message = default_message_for_failure_type(
        PatchFailureType.UNKNOWN_PATCH_FAILURE
    )
    assert result == {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": 1,
        "error": expected_message,
    }
    assert _run_row(run_id) == ("failed", "chunk_1_failed")
    assert _chunk_row(run_id) == ("failed", expected_message)
    assert _gate_counts(run_id) == {}
    # Zero auto-retries: one coder pass, one apply, no test run, no commit.
    assert _stage_counts(calls) == {
        "baseline": 1, "planner": 1, "coder": 1, "dry_run": 1, "patch": 1,
        "test": 0, "review": 0, "commit": 0,
    }
    assert _normalized_summary(run_id) == _failure_summary(
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
        technical_details="patch_applier.py: unexpected apply error",
        changed_files_attempted=FILES,
        suggested_actions=[
            "retry_with_instruction", "reject_chunk",
            "mark_manual_intervention", "view_details",
        ],
        rollback_performed=True,
        working_tree_clean=True,
        retry={"attempts": 0, "max_attempts": 0, "retryable": False},
        failed_step="patch",
        manual_intervention_needed=False,
        attempts=[
            _attempt(
                attempt_number=1,
                recovery_mode="initial",
                failure_type="UNKNOWN_PATCH_FAILURE",
                failed_step="patch",
                changed_files_attempted=FILES,
                changed_files_actual=[],
                scope_ok=True,
                test_outcome="not_run",
                outcome="failed",
                working_tree_clean=True,
                rollback_performed=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_golden_dirty_worktree(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    monkeypatch.setattr(
        chunked_orchestrator,
        "run_baseline_tests",
        lambda run_id_arg: make_test_result(run_id_arg, True),
    )
    # Preflight (ensure_clean_worktree) is faked clean; the per-chunk
    # precondition sees a dirty tree and must fail before any planner/coder work.
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: False
    )

    async def forbidden_planner(*args, **kwargs):
        raise AssertionError("planner must not run on a dirty tree")

    async def forbidden_coder(*args, **kwargs):
        raise AssertionError("coder must not run on a dirty tree")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", forbidden_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", forbidden_coder)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    expected_message = default_message_for_failure_type(
        PatchFailureType.DIRTY_WORKTREE
    )
    assert result == {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": 1,
        "error": expected_message,
    }
    assert _run_row(run_id) == ("failed", "chunk_1_failed")
    assert _chunk_row(run_id) == ("failed", expected_message)
    assert _gate_counts(run_id) == {}
    assert not any(c[0] == "commit" for c in calls)
    assert _normalized_summary(run_id) == _failure_summary(
        PatchFailureType.DIRTY_WORKTREE,
        technical_details=None,
        changed_files_attempted=[],
        suggested_actions=[
            "retry", "reject_chunk", "mark_manual_intervention", "view_details",
        ],
        rollback_performed=False,
        working_tree_clean=False,
        retry={"attempts": 0, "max_attempts": 0, "retryable": True},
        failed_step="patch",
        manual_intervention_needed=False,
        attempts=[
            _attempt(
                attempt_number=1,
                recovery_mode="initial",
                failure_type="DIRTY_WORKTREE",
                failed_step="patch",
                changed_files_attempted=[],
                changed_files_actual=[],
                scope_ok=True,
                test_outcome="not_run",
                outcome="failed",
                working_tree_clean=False,
                rollback_performed=False,
            ),
        ],
    )
