"""
test_stage_contract.py
Phase 2 item 10 — the uniform StageOutcome contract (impl brief §10.3).

Proves:
  - taxonomy-map totality: every PatchFailureType maps to exactly one
    OutcomeClass, with no silent default for future enum additions;
  - contract round-trip: each stage adapter, given the inputs the orchestrator
    passes today, returns a StageOutcome whose carried handoff /
    PatchFailureReport is identical to what the underlying function returns;
  - one source of truth: the orchestrator's test-failure helpers ARE the
    stage_contract helpers (no drifting copy).

No real AI, git mutation, or subprocesses — adapters are exercised against the
same fakes the orchestrator suite uses.
"""

import pytest

from backend.models.chunk import ChunkDefinition
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline import chunked_orchestrator, stage_contract
from backend.pipeline.patch_applier import PatchApplyOutcome, classify_patch_failure
from backend.pipeline.patch_dry_run import DryRunResult
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
)
from backend.pipeline.stage_contract import (
    OutcomeClass,
    StageOutcome,
    apply_stage,
    code_stage,
    outcome_class_for_failure,
    plan_stage,
    preflight_stage,
    review_stage,
    verify_stage,
)
from backend.pipeline.test_run_validation import ExecutionIntegrity
from backend.tests.test_chunked_orchestrator import (
    make_coder_result,
    make_empty_coder_result,
    make_failed_test_result,
    make_patch_result,
    make_planner_result,
    make_test_result,
)

pytestmark = pytest.mark.unit

RUN_ID = "stage-contract-run"
FILES = ["created_1.py", "modified_1.py", "deleted_1.py"]


def make_chunk() -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=1,
        title="Chunk 1",
        description="Do chunk 1",
        files_expected=list(FILES),
        depends_on=[],
        risk_level="low",
        token_estimate=100,
        requires_human_review=False,
        rationale="contract test",
    )


# --- Taxonomy map -------------------------------------------------------------


def test_outcome_class_map_is_total():
    """Every PatchFailureType member maps; nothing silently defaults."""
    for failure_type in PatchFailureType:
        outcome_class = outcome_class_for_failure(failure_type)
        assert isinstance(outcome_class, OutcomeClass)
        # Failure types can never classify as SUCCESS or NEEDS_HUMAN.
        assert outcome_class in {
            OutcomeClass.CODE_REJECTED,
            OutcomeClass.INFRA_ERROR,
            OutcomeClass.POLICY_BLOCKED,
        }


def test_outcome_class_map_assignments():
    """The exact class per failure type (impl brief §10.1)."""
    expected = {
        PatchFailureType.PATCH_MALFORMED: OutcomeClass.CODE_REJECTED,
        PatchFailureType.PATCH_DOES_NOT_APPLY: OutcomeClass.CODE_REJECTED,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED: OutcomeClass.CODE_REJECTED,
        PatchFailureType.TARGET_MISSING: OutcomeClass.CODE_REJECTED,
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED: OutcomeClass.CODE_REJECTED,
        PatchFailureType.NO_CHANGES: OutcomeClass.CODE_REJECTED,
        PatchFailureType.TEST_FAILURE_AFTER_APPLY: OutcomeClass.CODE_REJECTED,
        PatchFailureType.TEST_REGRESSION: OutcomeClass.CODE_REJECTED,
        PatchFailureType.SCOPE_VIOLATION: OutcomeClass.POLICY_BLOCKED,
        PatchFailureType.FORBIDDEN_FILE: OutcomeClass.POLICY_BLOCKED,
        PatchFailureType.DIRTY_WORKTREE: OutcomeClass.POLICY_BLOCKED,
        PatchFailureType.HARNESS_ERROR: OutcomeClass.INFRA_ERROR,
        PatchFailureType.UNKNOWN_PATCH_FAILURE: OutcomeClass.INFRA_ERROR,
    }
    assert {
        failure_type: outcome_class_for_failure(failure_type)
        for failure_type in PatchFailureType
    } == expected


# --- One source of truth -------------------------------------------------------


def test_orchestrator_helpers_are_the_stage_contract_helpers():
    """The orchestrator must share, not copy, the classification helpers."""
    assert (
        chunked_orchestrator._classify_test_failure
        is stage_contract.classify_test_failure
    )
    assert (
        chunked_orchestrator._build_test_failure_report
        is stage_contract.build_test_failure_report
    )


# --- Stage adapters: contract round-trip ----------------------------------------


@pytest.mark.asyncio
async def test_plan_stage_carries_planner_handoff_verbatim(monkeypatch):
    plan = make_planner_result(RUN_ID)
    seen = {}

    async def fake_planner(description, run_id, chunk_number=0, **kwargs):
        seen["args"] = (description, run_id, chunk_number, kwargs.get("project_id"))
        return plan

    monkeypatch.setattr(stage_contract, "run_planner", fake_planner)

    outcome = await plan_stage(
        "enriched", RUN_ID, chunk_number=1, project_id="proj"
    )

    assert seen["args"] == ("enriched", RUN_ID, 1, "proj")
    assert outcome.stage == "plan"
    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert outcome.success is True
    assert outcome.payload is plan
    assert outcome.failure is None


@pytest.mark.asyncio
async def test_code_stage_success_carries_coder_handoff_verbatim(monkeypatch):
    code = make_coder_result(RUN_ID, 1)

    async def fake_coder(plan, run_id, chunk_number=0, **kwargs):
        return code

    monkeypatch.setattr(stage_contract, "run_coder", fake_coder)

    outcome = await code_stage(
        make_planner_result(RUN_ID),
        RUN_ID,
        chunk_number=1,
        project_id="proj",
        files_expected=FILES,
    )

    assert outcome.stage == "code"
    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert outcome.payload is code
    assert outcome.failure is None


@pytest.mark.asyncio
async def test_code_stage_no_changes_matches_orchestrator_report(monkeypatch):
    empty = make_empty_coder_result(RUN_ID)

    async def fake_coder(plan, run_id, chunk_number=0, **kwargs):
        return empty

    monkeypatch.setattr(stage_contract, "run_coder", fake_coder)

    outcome = await code_stage(
        make_planner_result(RUN_ID),
        RUN_ID,
        chunk_number=1,
        project_id="proj",
        files_expected=FILES,
    )

    # Identical to the report _execute_single_chunk builds today.
    expected = build_patch_failure_report(
        PatchFailureType.NO_CHANGES,
        allowed_files=FILES,
        chunk_number=1,
        failed_step="patch",
    )
    assert outcome.outcome_class is OutcomeClass.CODE_REJECTED
    assert outcome.payload is empty
    assert outcome.failure == expected


def _fake_clean_tree(monkeypatch, clean: bool = True) -> None:
    # tmp_repo sits inside the developer's pipewright work tree, so the real
    # is_working_tree_clean would reflect unrelated local changes. Pin it, the
    # same way the orchestrator suite does.
    monkeypatch.setattr(
        stage_contract.local_git, "is_working_tree_clean", lambda repo: clean
    )


def test_preflight_stage_passes_in_scope_changes(monkeypatch, tmp_repo):
    _fake_clean_tree(monkeypatch)
    # Real dry-run: modify/delete targets must exist on disk; create must not.
    (tmp_repo / "modified_1.py").write_text("print('old')\n", encoding="utf-8")
    (tmp_repo / "deleted_1.py").write_text("print('bye')\n", encoding="utf-8")

    outcome = preflight_stage(
        make_coder_result(RUN_ID, 1),
        files_expected=FILES,
        target_repo_path=str(tmp_repo),
        chunk_number=1,
    )

    assert outcome.stage == "preflight"
    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert isinstance(outcome.payload, DryRunResult)
    assert outcome.payload.ok is True
    assert outcome.failure is None


def test_preflight_stage_scope_violation_matches_orchestrator_report(
    monkeypatch, tmp_repo
):
    _fake_clean_tree(monkeypatch)
    out_of_scope = CoderHandoff(
        run_id=RUN_ID,
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

    outcome = preflight_stage(
        out_of_scope,
        files_expected=FILES,
        target_repo_path=str(tmp_repo),
        chunk_number=1,
    )

    expected = build_patch_failure_report(
        PatchFailureType.SCOPE_VIOLATION,
        technical_details=(
            "scope_guard.py: coder wrote file outside approved scope. "
            "path=outside.py | "
            "files_expected=['created_1.py', 'deleted_1.py', 'modified_1.py']"
        ),
        changed_files_attempted=["outside.py"],
        allowed_files=FILES,
        working_tree_clean=True,  # non-git tmp path reads clean, as today
        chunk_number=1,
        failed_step="patch",
    )
    assert outcome.outcome_class is OutcomeClass.POLICY_BLOCKED
    assert outcome.failure == expected


def test_preflight_stage_dry_run_failure_matches_orchestrator_report(
    monkeypatch, tmp_repo
):
    _fake_clean_tree(monkeypatch)
    error_message = "[PATCH] cannot modify missing file: modified_1.py"
    monkeypatch.setattr(
        stage_contract,
        "dry_run_changes",
        lambda code, repo_path: DryRunResult(ok=False, error_message=error_message),
    )

    code = make_coder_result(RUN_ID, 1)
    outcome = preflight_stage(
        code,
        files_expected=FILES,
        target_repo_path=str(tmp_repo),
        chunk_number=1,
    )

    # Same classifier call the orchestrator makes on a dry-run failure.
    expected_type = classify_patch_failure(
        RuntimeError(error_message), phase="apply"
    )
    expected = build_patch_failure_report(
        expected_type,
        technical_details=error_message,
        changed_files_attempted=[change.path for change in code.files_changed],
        allowed_files=FILES,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="patch",
    )
    assert outcome.outcome_class is outcome_class_for_failure(expected_type)
    assert outcome.failure == expected
    assert outcome.payload.ok is False


def test_apply_stage_success_carries_outcome_verbatim(monkeypatch):
    apply_outcome = PatchApplyOutcome.from_success(make_patch_result(RUN_ID))
    monkeypatch.setattr(
        stage_contract,
        "apply_patch_guarded",
        lambda code, run_id, chunk_number=0, *, files_expected, repo_path=None: (
            apply_outcome
        ),
    )

    outcome = apply_stage(
        make_coder_result(RUN_ID, 1), RUN_ID, chunk_number=1, files_expected=FILES
    )

    assert outcome.stage == "apply"
    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert outcome.payload is apply_outcome
    assert outcome.failure is None


def test_apply_stage_failure_carries_report_verbatim(monkeypatch):
    failure = build_patch_failure_report(
        PatchFailureType.FORBIDDEN_FILE,
        technical_details="protected path",
        changed_files_attempted=["created_1.py"],
        allowed_files=FILES,
        rollback_performed=True,
        working_tree_clean=True,
        chunk_number=1,
        failed_step="patch",
    )
    apply_outcome = PatchApplyOutcome.from_failure(failure)
    monkeypatch.setattr(
        stage_contract,
        "apply_patch_guarded",
        lambda code, run_id, chunk_number=0, *, files_expected, repo_path=None: (
            apply_outcome
        ),
    )

    outcome = apply_stage(
        make_coder_result(RUN_ID, 1), RUN_ID, chunk_number=1, files_expected=FILES
    )

    assert outcome.outcome_class is OutcomeClass.POLICY_BLOCKED
    assert outcome.payload is apply_outcome
    assert outcome.failure is failure  # carried verbatim, not rebuilt


def test_verify_stage_pass_carries_test_result_verbatim(monkeypatch, tmp_repo):
    passed = make_test_result(RUN_ID, True)
    monkeypatch.setattr(
        stage_contract,
        "run_tests",
        lambda patch, run_id, chunk_number=0, **kwargs: passed,
    )

    outcome = verify_stage(
        make_patch_result(RUN_ID),
        RUN_ID,
        make_coder_result(RUN_ID, 1),
        make_chunk(),
        target_repo_path=str(tmp_repo),
    )

    assert outcome.stage == "verify"
    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert outcome.payload is passed
    assert outcome.failure is None
    assert outcome.integrity is None


@pytest.mark.parametrize(
    ("output", "exit_code", "timed_out", "expected_class", "expected_integrity"),
    [
        ("1 failed, 4 passed in 1.0s", 1, False,
         OutcomeClass.CODE_REJECTED, ExecutionIntegrity.OK),
        ("Fatal Python error: init_sys_streams", 1, False,
         OutcomeClass.INFRA_ERROR, ExecutionIntegrity.HARNESS_CRASH),
        ("[TESTER] command timed out", None, True,
         OutcomeClass.INFRA_ERROR, ExecutionIntegrity.TIMEOUT),
    ],
)
def test_verify_stage_failure_classifies_and_matches_orchestrator_report(
    monkeypatch, tmp_repo, output, exit_code, timed_out,
    expected_class, expected_integrity,
):
    _fake_clean_tree(monkeypatch)
    failed = make_failed_test_result(
        RUN_ID, output, exit_code=exit_code, timed_out=timed_out
    )
    monkeypatch.setattr(
        stage_contract,
        "run_tests",
        lambda patch, run_id, chunk_number=0, **kwargs: failed,
    )
    code = make_coder_result(RUN_ID, 1)
    chunk = make_chunk()

    outcome = verify_stage(
        make_patch_result(RUN_ID),
        RUN_ID,
        code,
        chunk,
        target_repo_path=str(tmp_repo),
        attempts=0,
        max_attempts=1,
    )

    expected_type, integrity = stage_contract.classify_test_failure(failed)
    expected = stage_contract.build_test_failure_report(
        expected_type,
        failed,
        code,
        chunk,
        clean=True,  # non-git tmp path reads clean, as today
        attempts=0,
        max_attempts=1,
    )
    assert outcome.outcome_class is expected_class
    assert outcome.integrity is expected_integrity
    assert integrity is expected_integrity
    assert outcome.payload is failed
    assert outcome.failure == expected
    assert outcome.evidence == (f"integrity={expected_integrity.value}",)


@pytest.mark.asyncio
async def test_review_stage_is_advisory_and_swallows_errors(monkeypatch):
    async def exploding_review(**kwargs):
        raise RuntimeError("reviewer exploded")

    monkeypatch.setattr(stage_contract, "run_chunk_review", exploding_review)

    outcome = await review_stage(
        RUN_ID,
        "proj",
        make_chunk(),
        make_coder_result(RUN_ID, 1),
        make_patch_result(RUN_ID),
        make_test_result(RUN_ID, True),
    )

    assert outcome.stage == "review"
    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert outcome.payload is None


@pytest.mark.asyncio
async def test_review_stage_carries_record(monkeypatch):
    sentinel = object()

    async def fake_review(**kwargs):
        return sentinel

    monkeypatch.setattr(stage_contract, "run_chunk_review", fake_review)

    outcome = await review_stage(
        RUN_ID,
        "proj",
        make_chunk(),
        make_coder_result(RUN_ID, 1),
        make_patch_result(RUN_ID),
        make_test_result(RUN_ID, True),
    )

    assert outcome.outcome_class is OutcomeClass.SUCCESS
    assert outcome.payload is sentinel


def test_stage_outcome_is_immutable():
    outcome = StageOutcome(stage="plan", outcome_class=OutcomeClass.SUCCESS)
    with pytest.raises(Exception):
        outcome.outcome_class = OutcomeClass.NEEDS_HUMAN
