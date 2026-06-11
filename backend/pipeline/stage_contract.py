"""
stage_contract.py
Phase 2 item 10/11 — uniform stage contract for chunk execution (proposal
§4.1/§4.2).

Defines the substrate the item-11 driver (chunk_driver.py) iterates:

  - OutcomeClass: the closed five-class outcome taxonomy (proposal §3 B-3).
  - outcome_class_for_failure: the single source of truth mapping the closed
    PatchFailureType taxonomy onto outcome classes. Total over the enum (a test
    iterates every member); an unmapped member fails loudly, never defaults.
  - StageOutcome: the one typed return every stage adapter produces. It WRAPS
    the existing PatchFailureReport / handoff objects verbatim — it never
    replaces them, so the stored completion_summary shape and its historical
    round-trip (patch_failure_report_from_completion_summary) are untouched.
  - Thin stage adapters behind the existing pure stage functions
    (run_planner / run_coder / assert_files_in_scope + dry_run_changes /
    apply_patch_guarded / run_tests / run_chunk_review). The wrapped functions
    keep their signatures, behavior, and tests; adapters only classify.
    Each adapter takes an optional ``*_fn`` callable so the driver can inject
    the orchestrator-resolved stage function (one namespace for fakes and for
    the strangler migration); the default is this module's import, so the
    item-10 contract and tests are unchanged.

The outcome class is a coarse narrative signal. It is NEVER the authority for
retryability or human-retry eligibility (impl brief §11.2a): auto-retry keys on
failure type + ExecutionIntegrity (item 8's rule), and human-retry eligibility
keeps deriving from the failure-type-level frozensets in patch_failures.py.

Also the canonical home of the post-apply test-failure classification helpers
(classify_test_failure / build_test_failure_report / verify_outcome_from_result)
so the orchestrator, the verify adapter, and the driver share one
implementation instead of drifting copies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.git import local_git
from backend.models.chunk import ChunkDefinition
from backend.models.handoff import (
    CoderHandoff,
    PatchResult,
    PipelineTestResult,
    PlannerHandoff,
)
from backend.pipeline.coder import run_coder
from backend.pipeline.patch_applier import (
    apply_patch_guarded,
    classify_patch_failure,
)
from backend.pipeline.patch_dry_run import dry_run_changes
from backend.pipeline.patch_failures import (
    PatchFailureReport,
    PatchFailureType,
    build_patch_failure_report,
)
from backend.pipeline.planner import run_planner
from backend.pipeline.reviewer import run_chunk_review
from backend.pipeline.scope_guard import ScopeDriftError, assert_files_in_scope
from backend.pipeline.test_run_validation import (
    ExecutionIntegrity,
    classify_execution_integrity,
)
from backend.pipeline.tester import run_tests

logger = logging.getLogger(__name__)


# Ordered stage list for one chunk (proposal §4.1). The driver (item 11)
# iterates this; in item 10 it is documentation-as-data only. gate_or_commit
# stays orchestrator-owned (gates, commits, and DB writes are not pure stages).
STAGE_PLAN = "plan"
STAGE_CODE = "code"
STAGE_PREFLIGHT = "preflight"
STAGE_APPLY = "apply"
STAGE_VERIFY = "verify"
STAGE_REVIEW = "review"
STAGE_GATE_OR_COMMIT = "gate_or_commit"

STAGE_SEQUENCE: tuple[str, ...] = (
    STAGE_PLAN,
    STAGE_CODE,
    STAGE_PREFLIGHT,
    STAGE_APPLY,
    STAGE_VERIFY,
    STAGE_REVIEW,
    STAGE_GATE_OR_COMMIT,
)


class OutcomeClass(str, Enum):
    """Closed five-class outcome taxonomy (proposal §3 Candidate B-3)."""

    SUCCESS = "SUCCESS"
    # The generated change is wrong (test regression, requirement mismatch).
    CODE_REJECTED = "CODE_REJECTED"
    # The world broke (harness crash, command not found, collection error).
    INFRA_ERROR = "INFRA_ERROR"
    # The rules said no (scope violation, forbidden path, dirty tree).
    POLICY_BLOCKED = "POLICY_BLOCKED"
    # Gates, acks, clarifications — a human must decide.
    NEEDS_HUMAN = "NEEDS_HUMAN"


# One source of truth mapping the closed PatchFailureType taxonomy onto
# outcome classes (impl brief §10.1). Total over the enum — a test iterates
# every member. NEEDS_HUMAN never comes from a failure type (it is produced by
# gates, not failures), and SUCCESS obviously cannot.
_OUTCOME_CLASS_BY_FAILURE_TYPE: dict[PatchFailureType, OutcomeClass] = {
    # The change is wrong for the current repo: regenerate or steer it.
    PatchFailureType.PATCH_MALFORMED: OutcomeClass.CODE_REJECTED,
    PatchFailureType.PATCH_DOES_NOT_APPLY: OutcomeClass.CODE_REJECTED,
    PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED: OutcomeClass.CODE_REJECTED,
    PatchFailureType.TARGET_MISSING: OutcomeClass.CODE_REJECTED,
    PatchFailureType.STALE_INDEX_OR_FILE_CHANGED: OutcomeClass.CODE_REJECTED,
    PatchFailureType.NO_CHANGES: OutcomeClass.CODE_REJECTED,
    # Legacy pre-split umbrella (item 8 kept it for stored-report back-compat);
    # historical rows classify like the regression half of the split.
    PatchFailureType.TEST_FAILURE_AFTER_APPLY: OutcomeClass.CODE_REJECTED,
    PatchFailureType.TEST_REGRESSION: OutcomeClass.CODE_REJECTED,
    # Deterministic safety blocks: never auto-retried, scope_guard is authority.
    PatchFailureType.SCOPE_VIOLATION: OutcomeClass.POLICY_BLOCKED,
    PatchFailureType.FORBIDDEN_FILE: OutcomeClass.POLICY_BLOCKED,
    PatchFailureType.DIRTY_WORKTREE: OutcomeClass.POLICY_BLOCKED,
    # The harness/environment failed, not the change. TIMEOUT also classifies
    # as HARNESS_ERROR today; its auto-retry exclusion is decided from
    # ExecutionIntegrity.TIMEOUT (item 8's rule), never from this map.
    PatchFailureType.HARNESS_ERROR: OutcomeClass.INFRA_ERROR,
    PatchFailureType.UNKNOWN_PATCH_FAILURE: OutcomeClass.INFRA_ERROR,
}


def outcome_class_for_failure(failure_type: PatchFailureType) -> OutcomeClass:
    """Map a PatchFailureType onto its OutcomeClass (total, never defaults)."""
    try:
        return _OUTCOME_CLASS_BY_FAILURE_TYPE[failure_type]
    except KeyError:
        raise LookupError(
            f"stage_contract.py: PatchFailureType.{failure_type.name} has no "
            "OutcomeClass mapping; add it to _OUTCOME_CLASS_BY_FAILURE_TYPE."
        ) from None


@dataclass(frozen=True)
class StageOutcome:
    """
    Uniform typed return of one stage pass (proposal §4.1).

    Wraps — never replaces — the underlying stage's native product:
      - payload: the stage's existing return, verbatim (PlannerHandoff,
        CoderHandoff, DryRunResult, PatchApplyOutcome, PipelineTestResult,
        ChunkReviewRecord, ...).
      - failure: the existing PatchFailureReport when the stage failed,
        byte-identical to what the orchestrator builds/persists today.
      - integrity: the verify stage's ExecutionIntegrity signal, carried so the
        driver can apply item 8's TIMEOUT-excluded auto-retry rule without
        re-parsing output.
      - evidence: short human-readable refs only — never output blobs or file
        contents (those stay on the report's capped technical_details).
      - checkpoint_payload: reserved for the driver's checkpoint
        write-and-verify (item 11). None here: the wrapped stages persist
        their own checkpoints today, exactly as before.
    """

    stage: str
    outcome_class: OutcomeClass
    payload: Any = None
    failure: PatchFailureReport | None = None
    integrity: ExecutionIntegrity | None = None
    evidence: tuple[str, ...] = ()
    checkpoint_payload: dict[str, Any] | None = None

    @property
    def success(self) -> bool:
        return self.outcome_class is OutcomeClass.SUCCESS


def classify_test_failure(
    test_result,
) -> tuple[PatchFailureType, ExecutionIntegrity]:
    """Split a failed post-apply test run into regression vs. harness (item 8)."""
    integrity = classify_execution_integrity(
        getattr(test_result, "exit_code", None),
        getattr(test_result, "output", None),
        timed_out=getattr(test_result, "timed_out", False),
    )
    if integrity is ExecutionIntegrity.OK:
        return PatchFailureType.TEST_REGRESSION, integrity
    return PatchFailureType.HARNESS_ERROR, integrity


def build_test_failure_report(
    failure_type: PatchFailureType,
    test_result,
    code: CoderHandoff,
    chunk: ChunkDefinition,
    *,
    clean: bool,
    attempts: int = 0,
    max_attempts: int | None = None,
) -> PatchFailureReport:
    """Build the post-apply test-failure report exactly as persisted today."""
    output = getattr(test_result, "output", None)
    exit_code = getattr(test_result, "exit_code", None)
    timed_out = getattr(test_result, "timed_out", False)
    details = (
        f"exit_code={exit_code}; timed_out={timed_out}\n{output}"
        if output is not None
        else f"exit_code={exit_code}; timed_out={timed_out}"
    )
    if getattr(test_result, "failing_test_ids", None):
        details += (
            "\nfailing_test_ids="
            + json.dumps(list(getattr(test_result, "failing_test_ids", [])))
        )
    if getattr(test_result, "newly_failing_test_ids", None):
        details += (
            "\nnewly_failing_test_ids="
            + json.dumps(list(getattr(test_result, "newly_failing_test_ids", [])))
        )
    parse_error = getattr(test_result, "failing_test_ids_parse_error", None)
    if parse_error:
        details += f"\nfailing_test_ids_parse_error={parse_error}"
    return build_patch_failure_report(
        failure_type,
        technical_details=details,
        changed_files_attempted=[change.path for change in code.files_changed],
        allowed_files=chunk.files_expected,
        rollback_performed=True,
        working_tree_clean=clean,
        attempts=attempts,
        max_attempts=max_attempts,
        chunk_number=chunk.chunk_number,
        failed_step="test",
    )


# --- Stage adapters ----------------------------------------------------------
# Thin wrappers over the existing stage functions. Each takes the inputs the
# orchestrator passes today and returns a StageOutcome carrying the underlying
# return / failure report verbatim. Unexpected exceptions propagate exactly as
# they do from the wrapped functions (the driver's chunk-level handler keeps
# owning them). The optional ``*_fn`` parameters exist for the driver, which
# injects the orchestrator-module-resolved callables; None means this module's
# own import (item-10 behavior).


async def plan_stage(
    enriched_description: str,
    run_id: str,
    *,
    chunk_number: int,
    project_id: str | None,
    run_planner_fn=None,
) -> StageOutcome:
    plan = await (run_planner_fn or run_planner)(
        enriched_description,
        run_id,
        chunk_number=chunk_number,
        project_id=project_id,
    )
    return StageOutcome(
        stage=STAGE_PLAN,
        outcome_class=OutcomeClass.SUCCESS,
        payload=plan,
    )


async def code_stage(
    plan: PlannerHandoff,
    run_id: str,
    *,
    chunk_number: int,
    project_id: str | None,
    files_expected: list[str],
    run_coder_fn=None,
    continuation_context: str | None = None,
) -> StageOutcome:
    # The steered continuation context (item 13) is forwarded only when set,
    # so the steer-less modes call the coder with the exact pre-item-13
    # signature (and fakes without the new kwarg keep working).
    extra_kwargs = (
        {"continuation_context": continuation_context}
        if continuation_context is not None
        else {}
    )
    code = await (run_coder_fn or run_coder)(
        plan,
        run_id,
        chunk_number=chunk_number,
        project_id=project_id,
        **extra_kwargs,
    )
    if not code.files_changed:
        report = build_patch_failure_report(
            PatchFailureType.NO_CHANGES,
            allowed_files=files_expected,
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return StageOutcome(
            stage=STAGE_CODE,
            outcome_class=outcome_class_for_failure(report.failure_type),
            payload=code,
            failure=report,
        )
    return StageOutcome(
        stage=STAGE_CODE,
        outcome_class=OutcomeClass.SUCCESS,
        payload=code,
    )


def preflight_stage(
    code: CoderHandoff,
    *,
    files_expected: list[str],
    target_repo_path: str,
    chunk_number: int,
    dry_run_fn=None,
) -> StageOutcome:
    """Scope pre-check then zero-mutation dry-run, before any write."""
    try:
        assert_files_in_scope(code, files_expected)
    except ScopeDriftError as drift:
        report = build_patch_failure_report(
            PatchFailureType.SCOPE_VIOLATION,
            technical_details=str(drift),
            changed_files_attempted=[change.path for change in code.files_changed],
            allowed_files=files_expected,
            working_tree_clean=local_git.is_working_tree_clean(target_repo_path),
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return StageOutcome(
            stage=STAGE_PREFLIGHT,
            outcome_class=outcome_class_for_failure(report.failure_type),
            failure=report,
        )

    dry = (dry_run_fn or dry_run_changes)(code, target_repo_path)
    if not dry.ok:
        failure_type = classify_patch_failure(
            RuntimeError(dry.error_message or ""), phase="apply"
        )
        report = build_patch_failure_report(
            failure_type,
            technical_details=dry.error_message,
            changed_files_attempted=[change.path for change in code.files_changed],
            allowed_files=files_expected,
            working_tree_clean=local_git.is_working_tree_clean(target_repo_path),
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return StageOutcome(
            stage=STAGE_PREFLIGHT,
            outcome_class=outcome_class_for_failure(failure_type),
            payload=dry,
            failure=report,
        )
    return StageOutcome(
        stage=STAGE_PREFLIGHT,
        outcome_class=OutcomeClass.SUCCESS,
        payload=dry,
    )


def apply_stage(
    code: CoderHandoff,
    run_id: str,
    *,
    chunk_number: int,
    files_expected: list[str],
    apply_fn=None,
) -> StageOutcome:
    outcome = (apply_fn or apply_patch_guarded)(
        code,
        run_id,
        chunk_number=chunk_number,
        files_expected=files_expected,
    )
    if not outcome.success:
        return StageOutcome(
            stage=STAGE_APPLY,
            outcome_class=outcome_class_for_failure(outcome.failure.failure_type),
            payload=outcome,
            failure=outcome.failure,
        )
    return StageOutcome(
        stage=STAGE_APPLY,
        outcome_class=OutcomeClass.SUCCESS,
        payload=outcome,
    )


def verify_outcome_from_result(
    test_result,
    code: CoderHandoff,
    chunk: ChunkDefinition,
    *,
    target_repo_path: str,
    attempts: int = 0,
    max_attempts: int | None = None,
) -> StageOutcome:
    """
    Classify an already-run test result into the verify StageOutcome.

    Split out of verify_stage for the item-11 driver: remediation (the T2
    rollback) is driver-owned and must happen BETWEEN the failed run and this
    classification, so the report's working_tree_clean reads the post-rollback
    tree exactly as it did when the tester rolled back itself. Pure aside from
    the cleanliness read; builds the identical report verify_stage built.
    """
    if test_result.passed:
        return StageOutcome(
            stage=STAGE_VERIFY,
            outcome_class=OutcomeClass.SUCCESS,
            payload=test_result,
        )
    failure_type, integrity = classify_test_failure(test_result)
    clean = local_git.is_working_tree_clean(target_repo_path)
    report = build_test_failure_report(
        failure_type,
        test_result,
        code,
        chunk,
        clean=clean,
        attempts=attempts,
        max_attempts=max_attempts,
    )
    return StageOutcome(
        stage=STAGE_VERIFY,
        outcome_class=outcome_class_for_failure(failure_type),
        payload=test_result,
        failure=report,
        integrity=integrity,
        evidence=(f"integrity={integrity.value}",),
    )


def verify_stage(
    patch_result: PatchResult,
    run_id: str,
    code: CoderHandoff,
    chunk: ChunkDefinition,
    *,
    target_repo_path: str,
    baseline_kwargs: dict[str, Any] | None = None,
    attempts: int = 0,
    max_attempts: int | None = None,
    run_tests_fn=None,
) -> StageOutcome:
    test_result = (run_tests_fn or run_tests)(
        patch_result,
        run_id,
        chunk_number=chunk.chunk_number,
        **(baseline_kwargs or {}),
    )
    return verify_outcome_from_result(
        test_result,
        code,
        chunk,
        target_repo_path=target_repo_path,
        attempts=attempts,
        max_attempts=max_attempts,
    )


async def review_stage(
    run_id: str,
    project_id: str | None,
    chunk: ChunkDefinition,
    code: CoderHandoff,
    patch: PatchResult,
    test_result: PipelineTestResult,
    *,
    run_review_fn=None,
) -> StageOutcome:
    """
    Advisory reviewer (display-only evidence). Always SUCCESS: it never changes
    the chunk outcome, gates nothing, and is fully swallowed on any failure —
    the same defense-in-depth contract as the orchestrator's wrapper today.
    """
    record = None
    try:
        record = await (run_review_fn or run_chunk_review)(
            run_id=run_id,
            project_id=project_id,
            chunk=chunk,
            code=code,
            patch=patch,
            test_result=test_result,
        )
    except Exception as error:
        logger.warning(
            "[STAGE] advisory review skipped (best-effort) | "
            "run_id=%s | chunk=%s | error=%s",
            run_id, chunk.chunk_number, error,
        )
    return StageOutcome(
        stage=STAGE_REVIEW,
        outcome_class=OutcomeClass.SUCCESS,
        payload=record,
    )
