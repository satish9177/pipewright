"""
test_steered_chunk.py
Phase 3 item 13 — steered attempts on failed chunks (the real
retry_with_instruction), the append-only run_turns log, and the combined
human+steered budget single-sourced in policy.

Pins the DECIDED behavior (brief §13.3):

  - a steered attempt is human_retry + a steer + continuation context: same
    approved plan, re-run from the code stage, pause at the unchanged chunk
    gate on success, rollback-to-clean + appended human attempt on failure;
  - the conservative §5.3 pre-check: an out-of-scope steer mention does not
    run until the human re-confirms or routes through #27; comparison is
    against EFFECTIVE scope (original ∪ approved expansions); scope_guard
    stays the hard authority at apply;
  - eligibility derives from _RETRY_WITH_INSTRUCTION_TYPES (TEST_REGRESSION
    now steerable per proposal §4.2; deterministic non-retryables still
    rejected); steered attempts never auto-retry internally;
  - one shared per-chunk human budget (policy.HUMAN_ATTEMPT_BUDGET), auto
    attempts excluded;
  - every executed steer writes exactly one append-only run_turns row linked
    to its chunk_attempts row; feature_description stays immutable; refusals
    mutate nothing.

Reuses the orchestrator suite's fake harness; no real AI, git mutation, or
GitHub.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import _ensure_run_turns_shape, engine
from backend.main import app
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline import chunk_driver, chunked_orchestrator, policy
from backend.pipeline.chunk_attempt_store import list_chunk_attempts
from backend.pipeline.patch_failures import (
    ACTION_RETRY,
    ACTION_RETRY_WITH_INSTRUCTION,
    MAX_HUMAN_RETRIES,
    RECOVERED_PATCH_REVIEW_KIND,
    RETRY_INELIGIBLE_CAP_EXHAUSTED,
    RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE,
    PatchFailureType,
    build_patch_failure_report,
    evaluate_patch_retry_eligibility,
    evaluate_patch_steer_eligibility,
    record_initial_attempt,
    record_retry_attempt,
    suggested_actions_for,
)
from backend.pipeline.run_turn_store import list_run_turns, record_run_turn
from backend.tests.test_chunked_orchestrator import (
    _seed_failed_chunk,
    create_run,
    make_coder_result,
    make_failed_test_result,
    patch_git_preflight,
    patch_retry_pipeline,
    reset_worktree_state,
)

pytestmark = pytest.mark.unit

STEER = "Use a clearer error message in the new function."


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "run_turns",
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
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id}
            )


def _spy_rollback(monkeypatch) -> list:
    rollbacks = []

    def fake_rollback(run_id_arg, chunk_number=0):
        rollbacks.append((run_id_arg, chunk_number))
        reset_worktree_state()
        return True

    monkeypatch.setattr(chunked_orchestrator, "rollback_patch", fake_rollback)
    return rollbacks


def _forbid_steer_execution(monkeypatch):
    """Make every execution seam explode, to prove refused == no work."""

    async def _boom_coder(*_a, **_k):
        raise AssertionError("a refused steer must not call the coder")

    def _boom(*_a, **_k):
        raise AssertionError("a refused steer must not run dry-run/apply/test")

    monkeypatch.setattr(chunked_orchestrator, "run_coder", _boom_coder)
    monkeypatch.setattr(chunked_orchestrator, "dry_run_changes", _boom)
    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", _boom)
    monkeypatch.setattr(chunked_orchestrator, "run_tests", _boom)


def _forbid_planner(monkeypatch):
    async def boom_planner(*_a, **_k):
        raise AssertionError("a steered attempt must not run the planner")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", boom_planner)


def _read_summary(run_id: str, chunk_number: int = 1) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT completion_summary FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()
    return json.loads(row[0])


def _feature_description(run_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT feature_description FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()[0]


def _steered_attempts(run_id: str, chunk_number: int = 1) -> list[dict]:
    return [
        attempt
        for attempt in list_chunk_attempts(run_id, chunk_number)
        if attempt["entry_mode"] == chunk_driver.EntryMode.STEERED.value
    ]


def _add_approved_scope_expansion(
    run_id: str, project_id: str, chunk_number: int, approved_files: list[str]
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO scope_expansion_requests (
                    id, run_id, project_id, chunk_number, failure_report_id,
                    requested_files, approved_files, status
                ) VALUES (
                    :id, :run_id, :project_id, :chunk_number, :frid,
                    :files, :files, 'approved'
                )
            """),
            {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "project_id": project_id,
                "chunk_number": chunk_number,
                "frid": str(uuid.uuid4()),
                "files": json.dumps(approved_files),
            },
        )


# --- Steered success / failure through the driver --------------------------- #


@pytest.mark.asyncio
async def test_steered_success_pauses_at_chunk_approval_and_logs_turn(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_failed_chunk(run_id, 1, frid, STEER)

    # Same gate as human_retry: success pauses, never commits.
    assert result["status"] == "awaiting_chunk_approval"
    assert not any(c[0] == "commit" for c in calls)
    assert [c[0] for c in calls if c[0] in {"coder", "dry_run", "patch", "test"}] == [
        "coder", "dry_run", "patch", "test",
    ]

    # Exactly one append-only steered ledger row.
    steered = _steered_attempts(run_id, 1)
    assert len(steered) == 1
    assert steered[0]["final_status"] == "awaiting_chunk_approval"
    assert steered[0]["final_outcome_class"] == "NEEDS_HUMAN"

    # Exactly one turn row, linked to that attempt, carrying the steer text.
    turns = list_run_turns(run_id)
    assert len(turns) == 1
    assert turns[0]["chunk_number"] == 1
    assert turns[0]["turn_number"] == 1
    assert turns[0]["steer_text"] == STEER
    assert turns[0]["attempt_id"] == steered[0]["id"]
    assert turns[0]["outcome"] == "awaiting_chunk_approval"

    # The audit anchor never moves.
    assert _feature_description(run_id) == "Execute chunks"

    # The pause summary records the attempt as a steered recovery.
    summary = _read_summary(run_id)
    assert summary["kind"] == RECOVERED_PATCH_REVIEW_KIND
    assert summary["attempts"][-1]["recovery_mode"] == "human_with_instruction"
    assert summary["attempts"][-1]["human_decision"] == "retry_with_instruction"


@pytest.mark.asyncio
async def test_steered_failure_rolls_back_clean_and_appends_steered_attempt(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        # No tester-side rollback: the driver must restore the tree.
        return make_failed_test_result(
            run_id_arg, "1 failed, 4 passed in 1.0s", exit_code=1
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)
    rollbacks = _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.steer_failed_chunk(run_id, 1, frid, STEER)

    assert result["status"] == "failed"
    # Tree clean after the failed steered attempt, exactly one rollback.
    assert chunked_orchestrator.local_git.is_working_tree_clean(
        project["repo_path"]
    ) is True
    assert rollbacks == [(run_id, 1)]

    steered = _steered_attempts(run_id, 1)
    assert len(steered) == 1
    assert steered[0]["final_status"] == "failed"

    turns = list_run_turns(run_id)
    assert len(turns) == 1
    assert turns[0]["attempt_id"] == steered[0]["id"]
    assert turns[0]["outcome"] == "failed"

    # The failure history gained exactly one steered human attempt.
    summary = _read_summary(run_id)
    assert summary["kind"] == "patch_failure"
    assert summary["attempts"][-1]["recovery_mode"] == "human_with_instruction"


@pytest.mark.asyncio
async def test_steered_attempt_never_auto_retries(
    monkeypatch, tmp_repo, tracked_runs
):
    """HARNESS_ERROR stays auto-retry's domain for fresh runs only: inside a
    steered attempt an infra-classified failure must not trigger the internal
    auto-retry loop (exactly one test invocation), like human_retry."""
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    def fake_tests(patch, run_id_arg, chunk_number=0):
        calls.append(("test", chunk_number))
        return make_failed_test_result(
            run_id_arg, "Fatal Python error: init_sys_streams", exit_code=1
        )

    monkeypatch.setattr(chunked_orchestrator, "run_tests", fake_tests)
    _spy_rollback(monkeypatch)

    result = await chunked_orchestrator.steer_failed_chunk(run_id, 1, frid, STEER)

    assert result["status"] == "failed"
    assert [c[0] for c in calls].count("test") == 1
    assert [c[0] for c in calls].count("coder") == 1


# --- Continuation context: carried as text, never as tree state ------------- #


@pytest.mark.asyncio
async def test_continuation_context_reaches_coder_as_text_with_clean_tree(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    # Prior attempt's record: coder handoff + the applied (since rolled back)
    # diff, exactly where the real stages checkpoint them.
    prior_code = make_coder_result(run_id, 1)
    save_checkpoint(
        run_id=run_id, step="code", output=prior_code.model_dump(),
        handoff_contract=prior_code.model_dump(), git_hash="pre-patch",
        tests_passed=False, step_completed=True, chunk_number=1,
    )
    prior_diff = "--- a/modified_1.py\n+++ b/modified_1.py\n-old\n+new"
    save_checkpoint(
        run_id=run_id, step="patch", output={"diff": prior_diff},
        handoff_contract={"diff": prior_diff}, git_hash="post-patch",
        tests_passed=False, step_completed=True, chunk_number=1,
    )

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    seen = {}
    resolved_coder = make_coder_result(run_id, 1)

    async def capturing_coder(plan, run_id_arg, chunk_number=0, **kwargs):
        seen["continuation_context"] = kwargs.get("continuation_context")
        seen["plan"] = plan
        # The prior diff travels as context only: the tree is clean at entry.
        seen["tree_clean_at_coder"] = (
            chunked_orchestrator.local_git.is_working_tree_clean(
                project["repo_path"]
            )
        )
        save_checkpoint(
            run_id=run_id_arg, step="code", output=resolved_coder.model_dump(),
            handoff_contract=resolved_coder.model_dump(), git_hash="pre-patch",
            tests_passed=False, step_completed=True, chunk_number=chunk_number,
        )
        return resolved_coder

    monkeypatch.setattr(chunked_orchestrator, "run_coder", capturing_coder)

    result = await chunked_orchestrator.steer_failed_chunk(run_id, 1, frid, STEER)

    assert result["status"] == "awaiting_chunk_approval"
    context = seen["continuation_context"]
    assert context is not None
    assert STEER in context
    assert prior_diff in context
    assert "TEST_REGRESSION" in context
    assert prior_code.summary in context
    assert seen["tree_clean_at_coder"] is True
    # Same approved plan: the retry plan surfaces files_expected, no planner.
    assert "modified_1.py" in seen["plan"].files_to_modify


@pytest.mark.asyncio
async def test_human_retry_passes_no_continuation_context(
    monkeypatch, tmp_repo, tracked_runs
):
    """Parity: the steer-less path calls the coder with the exact pre-item-13
    signature — no continuation_context kwarg at all."""
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.HARNESS_ERROR
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    seen = {}
    resolved_coder = make_coder_result(run_id, 1)

    async def capturing_coder(plan, run_id_arg, chunk_number=0, **kwargs):
        seen["kwargs"] = dict(kwargs)
        return resolved_coder

    monkeypatch.setattr(chunked_orchestrator, "run_coder", capturing_coder)

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, frid)

    assert result["status"] == "awaiting_chunk_approval"
    assert "continuation_context" not in seen["kwargs"]
    # And no turn row: the turn log records steers, not plain retries.
    assert list_run_turns(run_id) == []


# --- Conservative §5.3: out-of-scope steer mentions -------------------------- #


@pytest.mark.asyncio
async def test_out_of_scope_steer_mention_refuses_without_running(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_steer_execution(monkeypatch)

    result = await chunked_orchestrator.steer_failed_chunk(
        run_id, 1, frid, "Also fix utils.py while you are at it."
    )

    assert result["status"] == "steer_needs_scope_confirmation"
    assert result["status_code"] == 409
    assert result["out_of_scope_mentions"] == ["utils.py"]
    # Nothing ran, nothing mutated: chunk still failed, no attempt, no turn.
    assert _read_summary(run_id)["kind"] == "patch_failure"
    assert _steered_attempts(run_id, 1) == []
    assert list_run_turns(run_id) == []


@pytest.mark.asyncio
async def test_in_scope_steer_mention_proceeds_without_reconfirm(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    result = await chunked_orchestrator.steer_failed_chunk(
        run_id, 1, frid, "Change modified_1.py to handle the empty case."
    )

    assert result["status"] == "awaiting_chunk_approval"
    assert len(list_run_turns(run_id)) == 1


@pytest.mark.asyncio
async def test_steer_compares_against_effective_scope_not_original(
    monkeypatch, tmp_repo, tracked_runs
):
    """A path already approved via a #27 expansion is effective scope: the
    §5.3 check must not re-confirm it (trap c)."""
    run_id, project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    _add_approved_scope_expansion(run_id, project["id"], 1, ["utils.py"])
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)

    result = await chunked_orchestrator.steer_failed_chunk(
        run_id, 1, frid, "Move the helper into utils.py as approved."
    )

    assert result["status"] == "awaiting_chunk_approval"


@pytest.mark.asyncio
async def test_reconfirmed_steer_proceeds_and_scope_guard_still_blocks(
    monkeypatch, tmp_repo, tracked_runs
):
    """Explicit re-confirm runs the steer — but the steer granted nothing:
    a regenerated patch that actually touches the outside file is still
    killed by the unchanged scope pre-check (scope_guard backstop)."""
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    out_of_scope_coder = CoderHandoff(
        run_id=run_id,
        feature_description="enriched",
        files_changed=[
            FileChange(
                path="utils.py",
                action="create",
                content="print('outside')\n",
                reason="steer asked for it",
            ),
        ],
        summary="Touched a file outside approved scope",
        suggested_memory_entries=[],
    )
    patch_retry_pipeline(
        monkeypatch, run_id, calls=calls, coder_result=out_of_scope_coder
    )

    result = await chunked_orchestrator.steer_failed_chunk(
        run_id, 1, frid, "Also fix utils.py.", confirm_in_scope=True
    )

    assert result["status"] == "failed"
    summary = _read_summary(run_id)
    assert summary["failure_type"] == PatchFailureType.SCOPE_VIOLATION.value
    # The coder ran (re-confirm proceeds) but nothing was applied or tested.
    assert any(c[0] == "coder" for c in calls)
    assert not any(c[0] in {"patch", "test"} for c in calls)
    # The refused write still produced a turn (the steer DID run an attempt).
    assert len(list_run_turns(run_id)) == 1


# --- Eligibility ------------------------------------------------------------- #


def _steer_decision(failure_type, attempts=None):
    report = build_patch_failure_report(
        failure_type,
        allowed_files=["a.py"],
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="test",
    )
    report = record_initial_attempt(
        report,
        failure_report_id="frid-1",
        attempt_id="a-1",
        started_at="2026-06-12T00:00:00+00:00",
    )
    for mode in attempts or []:
        report = record_retry_attempt(
            report,
            failure_report_id="frid-1",
            attempt_id=str(uuid.uuid4()),
            started_at="2026-06-12T00:00:00+00:00",
            recovery_mode=mode,
        )
    return evaluate_patch_steer_eligibility(
        report,
        requested_failure_report_id="frid-1",
        dependencies_met=True,
        working_tree_clean=True,
        chunk_status="failed",
    )


def test_test_regression_is_steerable_but_not_plain_retryable():
    steer = _steer_decision(PatchFailureType.TEST_REGRESSION)
    assert steer.eligible is True

    report = build_patch_failure_report(
        PatchFailureType.TEST_REGRESSION,
        allowed_files=["a.py"],
        working_tree_clean=True,
        chunk_number=1,
        failed_step="test",
    )
    report = record_initial_attempt(
        report,
        failure_report_id="frid-1",
        attempt_id="a-1",
        started_at="2026-06-12T00:00:00+00:00",
    )
    retry = evaluate_patch_retry_eligibility(
        report,
        requested_failure_report_id="frid-1",
        dependencies_met=True,
        working_tree_clean=True,
        chunk_status="failed",
    )
    assert retry.eligible is False
    assert retry.reason == RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE

    actions = suggested_actions_for(
        PatchFailureType.TEST_REGRESSION, attempts=0, max_attempts=2
    )
    assert ACTION_RETRY_WITH_INSTRUCTION in actions
    assert ACTION_RETRY not in actions


@pytest.mark.parametrize(
    "failure_type",
    [
        PatchFailureType.SCOPE_VIOLATION,
        PatchFailureType.NO_CHANGES,
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        PatchFailureType.HARNESS_ERROR,
    ],
)
def test_steer_eligible_exactly_where_retry_with_instruction_is_advertised(
    failure_type,
):
    assert _steer_decision(failure_type).eligible is True


@pytest.mark.parametrize(
    "failure_type",
    [
        PatchFailureType.FORBIDDEN_FILE,
        PatchFailureType.DIRTY_WORKTREE,
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED,
    ],
)
def test_deterministic_non_retryables_stay_non_steerable(failure_type):
    decision = _steer_decision(failure_type)
    assert decision.eligible is False
    assert decision.reason == RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE


@pytest.mark.asyncio
async def test_ineligible_steer_runs_nothing(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.FORBIDDEN_FILE
    )
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_steer_execution(monkeypatch)

    result = await chunked_orchestrator.steer_failed_chunk(run_id, 1, frid, STEER)

    assert result["status"] == "retry_ineligible"
    assert result["reason"] == RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE
    assert list_run_turns(run_id) == []


# --- Budget: one combined human+steered budget, sourced from policy ---------- #


def test_budget_is_single_sourced_in_policy():
    assert MAX_HUMAN_RETRIES is policy.HUMAN_ATTEMPT_BUDGET


def test_human_and_steered_attempts_share_one_budget():
    # One plain human + one steered attempt = the whole default budget of 2.
    exhausted = _steer_decision(
        PatchFailureType.TEST_REGRESSION,
        attempts=["human", "human_with_instruction"],
    )
    assert exhausted.eligible is False
    assert exhausted.reason == RETRY_INELIGIBLE_CAP_EXHAUSTED
    assert exhausted.human_retry_attempts_used == policy.HUMAN_ATTEMPT_BUDGET


def test_auto_attempts_never_consume_the_human_budget():
    decision = _steer_decision(
        PatchFailureType.TEST_REGRESSION, attempts=["auto", "auto"]
    )
    assert decision.eligible is True
    assert decision.human_retry_attempts_used == 0


@pytest.mark.asyncio
async def test_steer_refused_at_budget_exhaustion_with_clear_narrative(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id,
        1,
        failure_type=PatchFailureType.TEST_REGRESSION,
        human_attempts=policy.HUMAN_ATTEMPT_BUDGET,
    )
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_steer_execution(monkeypatch)

    result = await chunked_orchestrator.steer_failed_chunk(run_id, 1, frid, STEER)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == RETRY_INELIGIBLE_CAP_EXHAUSTED
    assert _steered_attempts(run_id, 1) == []


# --- Steer text validation ---------------------------------------------------- #


@pytest.mark.asyncio
async def test_blank_and_overlong_steers_rejected_before_any_work(tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    with pytest.raises(chunked_orchestrator.SteerValidationError):
        await chunked_orchestrator.steer_failed_chunk(run_id, 1, "frid", "   ")
    with pytest.raises(chunked_orchestrator.SteerValidationError):
        await chunked_orchestrator.steer_failed_chunk(
            run_id, 1, "frid", "x" * (policy.MAX_STEER_TEXT_CHARS + 1)
        )
    assert list_run_turns(run_id) == []


# --- Turn log: additive migration, append-only, sanitized -------------------- #


def test_run_turns_migration_is_idempotent_and_preserves_rows(tracked_runs):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    recorded = record_run_turn(
        run_id=run_id,
        project_id="proj-x",
        chunk_number=1,
        steer_text="first steer",
        attempt_id=None,
        outcome="failed",
    )
    # Re-running the guarded migration on a live DB must not drop or mutate.
    with engine.connect() as conn:
        _ensure_run_turns_shape(conn)
        _ensure_run_turns_shape(conn)
        conn.commit()
    turns = list_run_turns(run_id)
    assert len(turns) == 1
    assert turns[0]["id"] == recorded["id"]
    assert turns[0]["steer_text"] == "first steer"


def test_turns_are_append_only_with_monotonic_numbers(tracked_runs):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    first = record_run_turn(
        run_id=run_id, project_id="p", chunk_number=1, steer_text="one"
    )
    second = record_run_turn(
        run_id=run_id, project_id="p", chunk_number=1, steer_text="two"
    )
    assert (first["turn_number"], second["turn_number"]) == (1, 2)
    turns = list_run_turns(run_id)
    assert [t["steer_text"] for t in turns] == ["one", "two"]
    # The historical row is byte-identical after the append.
    assert turns[0]["id"] == first["id"]
    assert turns[0]["created_at"] == first["created_at"]


def test_turn_store_redacts_secret_like_steer_text(tracked_runs):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    leaked = "use token " + "A" * 40 + " for the call"
    recorded = record_run_turn(
        run_id=run_id, project_id="p", chunk_number=1, steer_text=leaked
    )
    assert "A" * 40 not in recorded["steer_text"]
    assert "[REDACTED]" in recorded["steer_text"]


# --- Route -------------------------------------------------------------------- #


def test_steer_route_calls_steer_failed_chunk(monkeypatch):
    called = {}

    async def fake_steer(run_id, chunk_number, failure_report_id, steer_text, *, confirm_in_scope=False):
        called.update(
            run_id=run_id,
            chunk_number=chunk_number,
            failure_report_id=failure_report_id,
            steer_text=steer_text,
            confirm_in_scope=confirm_in_scope,
        )
        return {"status": "awaiting_chunk_approval", "run_id": run_id}

    monkeypatch.setattr("backend.routes.chunks.steer_failed_chunk", fake_steer)
    client = TestClient(app)

    response = client.post(
        "/runs/run-123/chunks/1/steer",
        json={
            "failure_report_id": "frid-1",
            "steer_text": STEER,
            "confirm_in_scope": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_chunk_approval"
    assert called == {
        "run_id": "run-123",
        "chunk_number": 1,
        "failure_report_id": "frid-1",
        "steer_text": STEER,
        "confirm_in_scope": True,
    }


def test_steer_route_maps_refusals_to_their_status_codes(monkeypatch):
    async def fake_scope_refusal(run_id, chunk_number, failure_report_id, steer_text, *, confirm_in_scope=False):
        return {
            "status": "steer_needs_scope_confirmation",
            "run_id": run_id,
            "chunk_number": chunk_number,
            "out_of_scope_mentions": ["utils.py"],
            "status_code": 409,
            "detail": "re-confirm or expand scope",
        }

    monkeypatch.setattr(
        "backend.routes.chunks.steer_failed_chunk", fake_scope_refusal
    )
    client = TestClient(app)
    response = client.post(
        "/runs/run-123/chunks/1/steer",
        json={"failure_report_id": "frid-1", "steer_text": STEER},
    )
    assert response.status_code == 409
    assert response.json()["status"] == "steer_needs_scope_confirmation"

    async def fake_validation_error(*_a, **_k):
        raise chunked_orchestrator.SteerValidationError("too long")

    monkeypatch.setattr(
        "backend.routes.chunks.steer_failed_chunk", fake_validation_error
    )
    response = client.post(
        "/runs/run-123/chunks/1/steer",
        json={"failure_report_id": "frid-1", "steer_text": STEER},
    )
    assert response.status_code == 422
