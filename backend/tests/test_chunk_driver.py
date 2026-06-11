"""
test_chunk_driver.py
Phase 2 item 11 — the chunk driver's own contract (impl brief §11.3/§11.5).

Focuses on what no other suite pins:

  - Rollback-move equivalence (T2, the highest-risk hunk): with tester.py no
    longer rolling back, the DRIVER leaves the tree clean after every failed
    attempt. Includes a detector for a *missing* rollback (tree left dirty)
    and for a *double* rollback (invocation count is exactly one per failed
    attempt), for the failed-run, timeout, auto-retry, and crashed-run paths —
    and the human-retry path, which shares the same driver helper.
  - The failure report is built AFTER the rollback, so working_tree_clean
    reads the restored tree exactly as it did when the tester rolled back.
  - The no-op commit guard still refuses to commit through the driver.
  - Resume is a driver entry mode: skip only on a verified checkpoint,
    fail closed (raise, never skip) on an unverifiable one.
  - human_retry is a driver entry mode; steered still refuses loudly for
    Phase 3, and auto_retry is internal-only.

Golden parity (statuses, commits, gates, stored reports, invocation counts)
lives in test_golden_chunk_execution.py; the auto-retry budget/exclusion rules
stay pinned in test_chunked_orchestrator.py. Reuses that suite's fake harness;
no real AI, git mutation, or GitHub.
"""

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition
from backend.pipeline import chunk_driver, chunked_orchestrator
from backend.pipeline.chunk_attempt_store import (
    list_chunk_attempts,
    record_chunk_attempt,
)
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.patch_applier import PatchApplyOutcome
from backend.pipeline.patch_failures import PatchFailureType
from backend.tests.test_chunked_orchestrator import (
    _seed_failed_chunk,
    add_test_checkpoint,
    create_run,
    make_failed_test_result,
    make_patch_result,
    make_test_result,
    patch_git_preflight,
    patch_retry_pipeline,
    patch_resume_git,
    patch_success_pipeline,
    reset_worktree_state,
)

pytestmark = pytest.mark.unit


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


def _run_row(run_id: str) -> tuple:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status, current_step FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()


def _chunk_status(run_id: str, chunk_number: int = 1) -> str:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()[0]


def _read_summary(run_id: str, chunk_number: int = 1) -> dict:
    import json

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT completion_summary FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()
    return json.loads(row[0])


def _spy_rollback(monkeypatch) -> list:
    """
    Replace the driver-resolved rollback with a spy that restores the fake
    clean-tree state. The returned list detects BOTH failure modes of the T2
    move: empty when a required rollback never ran (and the tree assertion
    catches the dirt), more than one entry per failed attempt on a double
    rollback.
    """
    rollbacks = []

    def fake_rollback(run_id_arg, chunk_number=0):
        rollbacks.append((run_id_arg, chunk_number))
        reset_worktree_state()
        return True

    monkeypatch.setattr(chunked_orchestrator, "rollback_patch", fake_rollback)
    return rollbacks


def _tree_is_clean(repo_path: str) -> bool:
    # Resolves the stateful fake installed by patch_success_pipeline.
    return chunked_orchestrator.local_git.is_working_tree_clean(repo_path)


# --- Rollback-move equivalence (T2) -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "exit_code", "timed_out", "expected_type"),
    [
        # Test regression: rolled back, never auto-retried.
        ("1 failed, 4 passed in 1.0s", 1, False, PatchFailureType.TEST_REGRESSION),
        # Timeout: harness error, rolled back, excluded from auto-retry.
        ("[TESTER] command timed out", None, True, PatchFailureType.HARNESS_ERROR),
    ],
)
async def test_failed_run_rolls_back_exactly_once_and_leaves_tree_clean(
    monkeypatch, tmp_repo, tracked_runs, output, exit_code, timed_out,
    expected_type,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        # tester.py no longer rolls back: the tree stays dirty from the apply
        # unless the DRIVER remediates.
        return make_failed_test_result(
            run_id_arg, output, exit_code=exit_code, timed_out=timed_out
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)
    rollbacks = _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    # Missing-rollback detector: the tree is clean again after the failure.
    assert _tree_is_clean(project["repo_path"]) is True
    # Double-rollback detector: exactly one rollback for the one failed attempt.
    assert rollbacks == [(run_id, 1)]
    assert [c[0] for c in calls].count("test") == 1
    summary = _read_summary(run_id)
    assert summary["failure_type"] == expected_type.value
    # Built AFTER the rollback: the report reads the restored tree, exactly as
    # it did when the tester performed the rollback itself.
    assert summary["working_tree_clean"] is True
    assert summary["rollback_performed"] is True


@pytest.mark.asyncio
async def test_auto_retry_rolls_back_each_failed_attempt(
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
        make_failed_test_result(
            run_id, "Fatal Python error: init_sys_streams", exit_code=1
        ),
    ]

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        return results.pop(0)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)
    rollbacks = _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    # One rollback per failed attempt: initial + the single budgeted auto retry.
    assert rollbacks == [(run_id, 1), (run_id, 1)]
    assert [c[0] for c in calls].count("test") == 2
    assert _tree_is_clean(project["repo_path"]) is True


@pytest.mark.asyncio
async def test_passed_run_never_rolls_back(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    rollbacks = _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert rollbacks == []


@pytest.mark.asyncio
async def test_crashed_test_run_still_rolls_back_and_fails_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def exploding_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        raise RuntimeError(
            f"tester.py: test execution failed. run_id={run_id_arg} | error=boom"
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", exploding_tests)
    rollbacks = _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert "test execution failed" in result["error"]
    assert rollbacks == [(run_id, 1)]
    assert _tree_is_clean(project["repo_path"]) is True
    assert _chunk_status(run_id) == "failed"


def test_run_tests_with_rollback_combined_failure_raises_clear_error(
    monkeypatch,
):
    def exploding_tests(patch, run_id_arg, chunk_number=0):
        raise RuntimeError("test harness exploded")

    def exploding_rollback(run_id_arg, chunk_number=0):
        raise RuntimeError("rollback exploded")

    monkeypatch.setattr(chunked_orchestrator, "run_tests", exploding_tests)
    monkeypatch.setattr(chunked_orchestrator, "rollback_patch", exploding_rollback)

    with pytest.raises(RuntimeError) as error:
        chunk_driver.run_tests_with_rollback(
            make_patch_result("run-x"), "run-x", 1, {}
        )

    assert "test execution failed and rollback failed" in str(error.value)
    assert "test harness exploded" in str(error.value)
    assert "rollback exploded" in str(error.value)


def test_run_tests_with_rollback_unit_semantics(monkeypatch):
    rollbacks = []
    monkeypatch.setattr(
        chunked_orchestrator,
        "rollback_patch",
        lambda run_id_arg, chunk_number=0: rollbacks.append(
            (run_id_arg, chunk_number)
        ) or True,
    )

    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda patch, run_id_arg, chunk_number=0: make_test_result(
            run_id_arg, True
        ),
    )
    passed = chunk_driver.run_tests_with_rollback(
        make_patch_result("run-x"), "run-x", 2, {}
    )
    assert passed.passed is True
    assert rollbacks == []

    monkeypatch.setattr(
        chunked_orchestrator,
        "run_tests",
        lambda patch, run_id_arg, chunk_number=0: make_test_result(
            run_id_arg, False
        ),
    )
    failed = chunk_driver.run_tests_with_rollback(
        make_patch_result("run-x"), "run-x", 2, {}
    )
    assert failed.passed is False
    assert rollbacks == [("run-x", 2)]


@pytest.mark.asyncio
async def test_human_retry_failed_tests_roll_back_via_driver(
    monkeypatch, tmp_repo, tracked_runs
):
    """
    The human-retry path routes the verify step through the driver's
    run_tests_with_rollback, so a failed retry run still restores a clean tree
    exactly once.
    """
    run_id, project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.HARNESS_ERROR
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        # No tester-side rollback anymore: the driver must restore the tree.
        return make_failed_test_result(
            run_id_arg, "1 failed, 4 passed in 1.0s", exit_code=1
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)
    rollbacks = _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "failed"
    assert rollbacks == [(run_id, 1)]
    assert _tree_is_clean(project["repo_path"]) is True


# --- No-op commit guard through the driver -------------------------------------


@pytest.mark.asyncio
async def test_no_effective_change_still_refuses_commit(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    def no_effect_apply(
        code, run_id_arg, chunk_number=0, *, files_expected, repo_path=None
    ):
        calls.append(("patch", chunk_number))
        # Apply "succeeds" but produces no on-disk change: tree stays clean.
        return PatchApplyOutcome.from_success(make_patch_result(run_id_arg))

    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", no_effect_apply)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert result["error"] == chunked_orchestrator.NO_EFFECTIVE_CHANGES_MESSAGE
    assert not any(c[0] == "commit" for c in calls)
    assert _chunk_status(run_id) == "failed"


# --- Resume entry mode ----------------------------------------------------------


def _definition(run_id: str, chunk_number: int = 1) -> ChunkDefinition:
    plan = get_chunk_plan_status(run_id)
    return chunked_orchestrator._definition_by_number(plan)[chunk_number]


@pytest.mark.asyncio
async def test_resume_mode_skips_verified_checkpoint_without_stages(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_message_exists",
        lambda repo, prefix: True,
    )

    async def boom(*_a, **_k):
        raise AssertionError("a verified checkpoint must skip every stage")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", boom)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", boom)

    plan = get_chunk_plan_status(run_id)
    drive = await chunk_driver.drive_chunk(
        chunk_driver.EntryMode.RESUME,
        run_id=run_id,
        project_id=project["id"],
        chunk=_definition(run_id),
        target_repo_path=project["repo_path"],
        branch_name=f"pipewright/{run_id[:8]}",
        status_by_number={1: "pending"},
        chunk_status=plan.chunks[0],
    )

    assert drive.skipped is True
    assert drive.pause is None
    assert _chunk_status(run_id) == "completed"


@pytest.mark.asyncio
async def test_resume_mode_fails_closed_on_unverifiable_checkpoint(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    # Checkpoint exists but the chunk commit cannot be found: never skip.
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "commit_message_exists",
        lambda repo, prefix: False,
    )

    plan = get_chunk_plan_status(run_id)
    with pytest.raises(RuntimeError, match="unsafe resume recovery"):
        await chunk_driver.drive_chunk(
            chunk_driver.EntryMode.RESUME,
            run_id=run_id,
            project_id=project["id"],
            chunk=_definition(run_id),
            target_repo_path=project["repo_path"],
            branch_name=f"pipewright/{run_id[:8]}",
            status_by_number={1: "pending"},
            chunk_status=plan.chunks[0],
        )

    # Fail-closed raises out of the resume; the chunk is never skip-completed.
    assert _run_row(run_id) == ("failed", "resume_recovery_failed")
    assert _chunk_status(run_id) != "completed"


@pytest.mark.asyncio
async def test_resume_mode_executes_stages_when_no_checkpoint(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    calls = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)

    plan = get_chunk_plan_status(run_id)
    drive = await chunk_driver.drive_chunk(
        chunk_driver.EntryMode.RESUME,
        run_id=run_id,
        project_id=project["id"],
        chunk=_definition(run_id),
        target_repo_path=project["repo_path"],
        branch_name=f"pipewright/{run_id[:8]}",
        status_by_number={1: "pending"},
        chunk_status=plan.chunks[0],
    )

    assert drive.skipped is False
    assert drive.pause is None
    assert any(c[0] == "planner" for c in calls)
    assert any(c[0] == "commit" for c in calls)
    assert _chunk_status(run_id) == "completed"


# --- Entry-mode dispatch seams ---------------------------------------------------


def _seam_chunk() -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=1,
        title="Chunk 1",
        description="Do chunk 1",
        files_expected=["a.py"],
        depends_on=[],
        risk_level="low",
        token_estimate=1,
        requires_human_review=False,
        rationale="entry-mode seam test",
    )


@pytest.mark.asyncio
async def test_human_retry_uses_driver_without_planner_and_records_attempt(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.HARNESS_ERROR
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    async def boom_planner(*_a, **_k):
        raise AssertionError("human_retry must not run planner")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", boom_planner)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_hash",
        lambda repo: "retry-head",
    )

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "awaiting_chunk_approval"
    assert not any(call[0] == "planner" for call in calls)
    assert [c[0] for c in calls if c[0] in {"coder", "dry_run", "patch", "test"}] == [
        "coder",
        "dry_run",
        "patch",
        "test",
    ]
    attempts = list_chunk_attempts(run_id, 1)
    assert len(attempts) == 1
    assert attempts[0]["entry_mode"] == chunk_driver.EntryMode.HUMAN_RETRY.value
    assert attempts[0]["final_status"] == "awaiting_chunk_approval"
    assert attempts[0]["final_outcome_class"] == "NEEDS_HUMAN"
    assert attempts[0]["head_sha"] == "retry-head"


@pytest.mark.asyncio
async def test_resume_fails_closed_when_attempt_head_drifted(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    record_chunk_attempt(
        run_id=run_id,
        project_id=project["id"],
        chunk_number=1,
        entry_mode=chunk_driver.EntryMode.FRESH.value,
        stage_outcomes=[],
        final_outcome_class="SUCCESS",
        final_status="completed",
        head_sha="expected-head",
    )
    patch_resume_git(monkeypatch)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_hash",
        lambda repo: "actual-head",
    )

    with pytest.raises(RuntimeError, match="HEAD does not match"):
        await chunked_orchestrator.resume_chunked_pipeline(run_id)


@pytest.mark.asyncio
async def test_resume_proceeds_when_attempt_head_matches(
    monkeypatch, tmp_repo, tracked_runs
):
    """
    P7 no-false-positive (brief §12.3): when the branch is exactly where the
    last completed attempt left it, the drift check must NOT dead-end resume.
    Also covers the pre-ledger degradation: no recorded HEAD -> no block.
    """
    run_id, project = create_run(tmp_repo, tracked_runs)
    record_chunk_attempt(
        run_id=run_id,
        project_id=project["id"],
        chunk_number=1,
        entry_mode=chunk_driver.EntryMode.FRESH.value,
        stage_outcomes=[],
        final_outcome_class="SUCCESS",
        final_status="completed",
        head_sha="expected-head",
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_hash",
        lambda repo: "expected-head",
    )
    # HEAD matches the last completed attempt -> must not raise.
    chunked_orchestrator._verify_resume_head_matches_last_attempt(
        run_id, str(tmp_repo)
    )

    # Pre-ledger / partially recorded run (no completed HEAD rows) -> degrade to
    # legacy behavior, never invent a block. Use a fresh run with no attempts.
    other_run_id, _other_project = create_run(tmp_repo, tracked_runs)
    chunked_orchestrator._verify_resume_head_matches_last_attempt(
        other_run_id, str(tmp_repo)
    )


@pytest.mark.asyncio
async def test_entry_modes_refuse_invalid_external_dispatch():
    kwargs = dict(
        run_id="run-x",
        project_id="proj-x",
        chunk=_seam_chunk(),
        target_repo_path="unused",
        branch_name="pipewright/run-x",
        status_by_number={},
    )

    # steered is implemented (item 13) but requires retry-specific context
    # plus the continuation context — bare dispatch refuses loudly.
    with pytest.raises(ValueError):
        await chunk_driver.drive_chunk(chunk_driver.EntryMode.STEERED, **kwargs)
    # auto_retry is internal to the driver loop, never an external entry.
    with pytest.raises(ValueError):
        await chunk_driver.drive_chunk(chunk_driver.EntryMode.AUTO_RETRY, **kwargs)
    # resume requires the chunk_status row backing checkpoint verification.
    with pytest.raises(ValueError):
        await chunk_driver.drive_chunk(chunk_driver.EntryMode.RESUME, **kwargs)
    # human_retry is implemented, but requires retry-specific context.
    with pytest.raises(ValueError):
        await chunk_driver.drive_chunk(chunk_driver.EntryMode.HUMAN_RETRY, **kwargs)
