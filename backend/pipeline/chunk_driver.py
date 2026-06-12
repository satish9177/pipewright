"""
chunk_driver.py
Phase 2 item 12 - the one driver for chunk execution (proposal section 4.1).

Collapses the per-chunk stage sequence that previously existed in three copies
(`_execute_single_chunk`, its inline auto-retry, and human retry)
into one loop over the item-10 stage contract. The driver serves `fresh`,
`resume`, `human_retry`, and `steered` entry modes plus the bounded INFRA
auto-retry internal to the loop. `steered` (Phase 3 item 13) is `human_retry`
plus a continuation-context block threaded into the code stage: same approved
plan, same files_expected, same gates, same rollback, same budget accounting.
The steer is advisory context inside the approved chunk — never an authority
channel for scope, approval, or Git decisions.

The driver owns the cross-cutting concerns, identically in every mode:

  - dependency + dirty-tree preconditions before any planner/coder/patch work;
  - scope pre-check and zero-mutation dry-run before any write (preflight);
  - the INFRA auto-retry loop: budget from policy.AUTO_RETRY_INFRA_BUDGET,
    gated on the verify stage's HARNESS_ERROR failure type with TIMEOUT
    excluded (item 8's rule) — NEVER on OutcomeClass.INFRA_ERROR alone, so an
    apply-phase UNKNOWN_PATCH_FAILURE is never auto-retried (brief §11.2a);
  - rollback-to-clean on any failed verify attempt (T2: the rollback call site
    moved here from tester.py — same trigger, a failed or crashed test run,
    same result);
  - test-verdict persistence for both pass and fail;
  - pause/gate returns and the resume checkpoint skip discipline
    (`_verify_completed_checkpoint_safe`, verbatim, fail-closed).

Strangler seam: stage callables and persistence/gate helpers are resolved
through the chunked_orchestrator module AT CALL TIME (`_orch()`), not imported
here. That keeps one namespace as the source of truth while the migration is
in flight — behavior, provenance, and the existing test fakes (which patch
chunked_orchestrator attributes) stay byte-identical. Item 12 continues the
migration; do not "clean this up" into direct imports before then.

The outcome class carried on each StageOutcome is a narrative signal only. It
is never the authority for retryability (auto-retry keys on failure type +
ExecutionIntegrity; human-retry eligibility stays with the frozensets in
patch_failures.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from backend.git import local_git
from backend.models.chunk import ChunkDefinition, ChunkStatus
from backend.pipeline.chunk_attempt_store import record_chunk_attempt
from backend.pipeline.patch_failures import (
    PatchFailureReport,
    PatchFailureType,
    build_patch_failure_report,
)
from backend.pipeline.policy import AUTO_RETRY_INFRA_BUDGET
from backend.pipeline.stage_contract import (
    OutcomeClass,
    STAGE_PLAN,
    StageOutcome,
    apply_stage,
    code_stage,
    plan_stage,
    preflight_stage,
    review_stage,
    verify_outcome_from_result,
)
from backend.pipeline.stage_profile import StageProfile

logger = logging.getLogger(__name__)


def _orch():
    """Resolve the orchestrator module at call time (see module docstring)."""
    from backend.pipeline import chunked_orchestrator

    return chunked_orchestrator


class EntryMode(str, Enum):
    """How a driver pass over a chunk was entered (proposal §4.1 table)."""

    FRESH = "fresh"
    RESUME = "resume"
    # Internal to the driver loop: never a valid external entry.
    AUTO_RETRY = "auto_retry"
    HUMAN_RETRY = "human_retry"
    # human_retry + a steer string in the continuation context (item 13).
    STEERED = "steered"


# The two human-triggered attempt modes share one execution path: re-run from
# the code stage with the same approved plan, pause at the chunk gate on
# success, append a human attempt on failure, and never auto-retry internally.
# steered differs only in the carried continuation context and its
# recovery_mode in the attempt history (shared budget, distinct audit label).
_HUMAN_ATTEMPT_MODES = frozenset({EntryMode.HUMAN_RETRY, EntryMode.STEERED})

_RECOVERY_MODE_BY_ENTRY_MODE = {
    EntryMode.HUMAN_RETRY: "human",
    EntryMode.STEERED: "human_with_instruction",
}


@dataclass(frozen=True)
class ChunkDriveResult:
    """
    Outcome of one driver pass over one chunk.

    pause:   a run-level result dict to bubble up (gate pause or failure) —
             exactly what _execute_single_chunk returned when not None.
    skipped: resume mode only — the chunk was skip-completed from a verified
             test checkpoint without re-executing any stage.

    pause None and skipped False means the chunk completed (committed).
    """

    pause: dict | None = None
    skipped: bool = False


@dataclass(frozen=True)
class RefinementContext:
    """
    Marks a STEERED pass as a post-success refinement of a *completed* chunk
    (item 14), and carries what restore needs if the refinement fails/is rejected.

    Its presence inverts exactly two driver behaviors versus an item-13 steer of
    a failed chunk: a failed attempt is finalized as *restore-to-completed* (the
    §0 invariant — the committed chunk and its commit survive) instead of
    mark-failed, and the success pause stores the refinement marker (which carries
    no failure report) instead of the recovered-patch marker. Everything else —
    the same approved plan, scope, preflight, apply, rollback-on-failed-verify,
    verdict persistence, and gates — is shared with the item-13 path unchanged.

    head_before is the branch tip captured at steer start; the §0 finalizer
    asserts the tree is restored to it. The remaining fields are threaded onto the
    pause marker so the orchestrator's approve/reject paths can restore run state.
    """

    chunk_commit_sha: str | None
    head_before: str
    prior_run_status: str
    prior_run_step: str | None
    final_gate_superseded: bool
    prior_completion_summary: dict | None


def run_tests_with_rollback(
    patch_result,
    run_id: str,
    chunk_number: int,
    baseline_kwargs: dict,
):
    """
    Run the test command and roll back the applied patch on any failed or
    crashed run (T2). Synchronous — callers offload via asyncio.to_thread so
    the rollback runs on the same worker thread the tests ran on, exactly as
    it did when tester.py performed it.

    Triggers are unchanged from the tester's previous side-effect: a failed
    result (including timeout), or test execution raising. A rollback error on
    the failed-result path propagates as before; on the crash path the
    combined error is raised so the original failure is never masked.
    """
    orch = _orch()
    try:
        test_result = orch.run_tests(
            patch_result,
            run_id,
            chunk_number=chunk_number,
            **baseline_kwargs,
        )
    except Exception as error:
        try:
            orch.rollback_patch(run_id, chunk_number)
        except Exception as rollback_error:
            raise RuntimeError(
                f"chunk_driver.py: test execution failed and rollback failed. "
                f"run_id={run_id} | error={error} | "
                f"rollback_error={rollback_error}"
            )
        raise
    if not test_result.passed:
        orch.rollback_patch(run_id, chunk_number)
    return test_result


def _current_head_or_none(target_repo_path: str) -> str | None:
    """Best-effort HEAD capture for the audit ledger."""
    try:
        return local_git.get_current_hash(target_repo_path)
    except Exception as error:
        logger.warning(
            "chunk attempt HEAD capture skipped | repo=%s | error=%s",
            target_repo_path,
            error,
        )
        return None


def _stage_outcome_record(outcome: StageOutcome) -> dict:
    failure = outcome.failure
    record = {
        "stage": outcome.stage,
        "outcome_class": outcome.outcome_class.value,
    }
    if failure is not None:
        record.update({
            "failure_type": failure.failure_type.value,
            "failed_step": failure.failed_step,
        })
    if outcome.integrity is not None:
        record["integrity"] = outcome.integrity.value
    if outcome.evidence:
        record["evidence"] = list(outcome.evidence)
    return record


def _record_attempt(
    *,
    run_id: str,
    project_id: str,
    chunk_number: int,
    entry_mode: EntryMode,
    stage_outcomes: list[StageOutcome],
    final_outcome_class: OutcomeClass,
    final_status: str,
    target_repo_path: str,
    stage_profile: StageProfile | None = None,
    trivial_profile_eligible: bool | None = None,
    head_sha: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict:
    """Append one metadata-only chunk_attempts row."""
    try:
        return record_chunk_attempt(
            run_id=run_id,
            project_id=project_id,
            chunk_number=chunk_number,
            entry_mode=entry_mode.value,
            stage_outcomes=[
                _stage_outcome_record(outcome) for outcome in stage_outcomes
            ],
            evidence_refs=evidence_refs or [],
            final_outcome_class=final_outcome_class.value,
            final_status=final_status,
            stage_profile=stage_profile.value if stage_profile is not None else None,
            trivial_profile_eligible=trivial_profile_eligible,
            head_sha=head_sha or _current_head_or_none(target_repo_path),
        )
    except Exception as error:
        logger.warning(
            "chunk attempt ledger write skipped | run_id=%s | chunk=%s | "
            "entry_mode=%s | error=%s",
            run_id,
            chunk_number,
            entry_mode.value,
            error,
        )
        return {}


def _gate_outcome(outcome_class: OutcomeClass) -> StageOutcome:
    return StageOutcome(stage="gate_or_commit", outcome_class=outcome_class)


def _precondition_outcome(
    outcome_class: OutcomeClass,
    *,
    failure: PatchFailureReport | None = None,
    evidence: tuple[str, ...] = (),
) -> StageOutcome:
    return StageOutcome(
        stage="precondition",
        outcome_class=outcome_class,
        failure=failure,
        evidence=evidence,
    )


def _resume_skip_or_none(
    run_id: str,
    chunk: ChunkDefinition,
    chunk_status: ChunkStatus,
    target_repo_path: str,
    status_by_number: dict[int, str],
) -> ChunkDriveResult | None:
    """
    Resume-mode checkpoint phase, moved verbatim from
    _resume_chunked_pipeline_locked: skip-complete a chunk only on a verified
    test checkpoint; fail closed otherwise. Returns None when there is no test
    checkpoint, meaning the chunk must execute fresh stages.

    A checkpoint that cannot be verified raises out of the driver (after
    marking the run failed/resume_recovery_failed) — it is NEVER converted to
    a per-chunk failure, preserving the resume fail-closed contract.
    """
    orch = _orch()
    chunk_number = chunk.chunk_number
    checkpoint = orch.load_chunk_step_checkpoint(run_id, chunk_number, "test")
    if checkpoint is None:
        return None

    # Dependency guard before skip-completing (#24A): a valid test checkpoint
    # must not mark a chunk completed while a dependency is still incomplete.
    unmet = orch._unmet_dependencies(chunk, status_by_number)
    if unmet:
        return ChunkDriveResult(
            pause=orch._fail_chunk(
                run_id,
                chunk_number,
                orch._dependency_not_met_message(
                    chunk_number, unmet, status_by_number
                ),
            )
        )
    try:
        orch._verify_completed_checkpoint_safe(
            run_id,
            chunk,
            chunk_status,
            target_repo_path,
        )
        orch.update_chunk_status(run_id, chunk_number, "completed")
        orch._update_run_status(
            run_id,
            "running",
            f"chunk_{chunk_number}_skipped",
            chunk_number,
        )
        return ChunkDriveResult(skipped=True)
    except Exception as error:
        orch._update_run_status(run_id, "failed", "resume_recovery_failed")
        raise RuntimeError(str(error))


def _finalize_failed_attempt(
    run_id: str,
    chunk_number: int,
    report: PatchFailureReport,
    code,
    auto_retry_base: PatchFailureReport | None,
    *,
    mode: EntryMode,
    prior_attempts: list | None = None,
    refinement: RefinementContext | None = None,
    target_repo_path: str | None = None,
) -> dict:
    """
    Persist a failed attempt and mark the chunk failed.

    On the auto-retry attempt the report is first folded into the carried
    attempt history (recovery_mode="auto"), exactly as the deleted inline
    retry did at each of its failure sites.

    Item 14: when ``refinement`` is set this is a post-success steer of a
    *completed* chunk, so marking the chunk failed would destroy a committed,
    human-approved chunk (the §0 invariant). The attempt has already been
    recorded in the ledger by the caller; here we hand off to the orchestrator's
    restore-to-completed finalizer instead of any mark-failed path.
    """
    orch = _orch()
    if refinement is not None:
        return orch._finalize_failed_refinement(
            run_id,
            chunk_number,
            refinement=refinement,
            target_repo_path=target_repo_path,
            message=report.message,
        )
    if mode in _HUMAN_ATTEMPT_MODES:
        return orch._persist_retry_patch_failure(
            run_id,
            chunk_number,
            report,
            list(prior_attempts or []),
            test_outcome="failed" if report.failed_step == "test" else "not_run",
            recovery_mode=_RECOVERY_MODE_BY_ENTRY_MODE[mode],
        )
    if auto_retry_base is not None:
        report = orch._record_auto_retry_result(
            auto_retry_base,
            report,
            outcome=(
                "manual_intervention"
                if report.manual_intervention_needed
                else "failed"
            ),
            code=code,
        )
    return orch._fail_chunk_with_report(run_id, chunk_number, report)



async def _drive_stages(
    mode: EntryMode,
    run_id: str,
    project_id: str,
    chunk: ChunkDefinition,
    target_repo_path: str,
    branch_name: str,
    status_by_number: dict[int, str],
    verification_baseline: dict | None,
    retry_report: PatchFailureReport | None = None,
    prior_attempts: list | None = None,
    run_step: str | None = None,
    continuation_context: str | None = None,
    refinement: RefinementContext | None = None,
    stage_profile: StageProfile | None = None,
    trivial_profile_eligible: bool | None = None,
) -> dict | None:
    """
    Drive one chunk attempt. human_retry and steered start after planning
    (same approved plan, never the planner); fresh/resume start at planning.
    The public return remains the old pause-or-None shape.

    When ``refinement`` is set (item 14) the pass is a post-success steer of a
    completed chunk: same machinery as a steered attempt, but every failure path
    restores the chunk to completed (the §0 invariant) and the success pause uses
    the refinement marker. See RefinementContext.
    """
    orch = _orch()
    chunk_number = chunk.chunk_number
    original_entry_mode = mode
    ledger_stage_profile = stage_profile if mode is EntryMode.FRESH else None
    ledger_trivial_profile_eligible = (
        trivial_profile_eligible if mode is EntryMode.FRESH else None
    )

    unmet = orch._unmet_dependencies(chunk, status_by_number)
    if unmet:
        outcome = _precondition_outcome(
            OutcomeClass.POLICY_BLOCKED,
            evidence=("dependency_not_met",),
        )
        _record_attempt(
            run_id=run_id,
            project_id=project_id,
            chunk_number=chunk_number,
            entry_mode=original_entry_mode,
            stage_outcomes=[outcome],
            final_outcome_class=OutcomeClass.POLICY_BLOCKED,
            final_status="failed",
            target_repo_path=target_repo_path,
            stage_profile=ledger_stage_profile,
            trivial_profile_eligible=ledger_trivial_profile_eligible,
        )
        message = orch._dependency_not_met_message(
            chunk_number, unmet, status_by_number
        )
        # A completed chunk's dependencies are by definition already completed, so
        # this is unreachable for a refinement — but a completed chunk must never
        # be markable failed from ANY driver path (§0).
        if refinement is not None:
            return orch._finalize_failed_refinement(
                run_id,
                chunk_number,
                refinement=refinement,
                target_repo_path=target_repo_path,
                message=message,
            )
        return orch._fail_chunk(run_id, chunk_number, message)

    orch.update_chunk_status(run_id, chunk_number, "running")
    orch._update_run_status(
        run_id,
        "running_chunks",
        run_step or f"chunk_{chunk_number}",
        chunk_number,
    )

    if not local_git.is_working_tree_clean(target_repo_path):
        report = build_patch_failure_report(
            PatchFailureType.DIRTY_WORKTREE,
            allowed_files=chunk.files_expected,
            working_tree_clean=False,
            chunk_number=chunk_number,
            failed_step="patch",
        )
        outcome = _precondition_outcome(
            OutcomeClass.POLICY_BLOCKED,
            failure=report,
            evidence=("dirty_worktree",),
        )
        _record_attempt(
            run_id=run_id,
            project_id=project_id,
            chunk_number=chunk_number,
            entry_mode=original_entry_mode,
            stage_outcomes=[outcome],
            final_outcome_class=outcome.outcome_class,
            final_status="failed",
            target_repo_path=target_repo_path,
            stage_profile=ledger_stage_profile,
            trivial_profile_eligible=ledger_trivial_profile_eligible,
        )
        # Eligibility verified a clean tree under the same lock moments ago, so a
        # refinement cannot reach here in practice; if it ever did, restore the
        # completed chunk rather than fail it (§0).
        if refinement is not None:
            return orch._finalize_failed_refinement(
                run_id,
                chunk_number,
                refinement=refinement,
                target_repo_path=target_repo_path,
                message=report.message,
            )
        return orch._fail_chunk_with_report(run_id, chunk_number, report)

    base_stage_outcomes: list[StageOutcome] = []
    if mode in _HUMAN_ATTEMPT_MODES:
        if retry_report is None and refinement is None:
            raise ValueError(
                f"chunk_driver.py: {mode.value} mode requires the current "
                "PatchFailureReport (or a RefinementContext for a post-success "
                "steer)."
            )
        plan = orch._retry_plan_for_chunk(run_id, chunk)
    else:
        enriched_description = orch._build_enriched_feature_description(
            run_id,
            project_id,
            chunk,
        )
        if ledger_stage_profile is StageProfile.MERGED_PLAN_CODE:
            plan_outcome = StageOutcome(
                stage=STAGE_PLAN,
                outcome_class=OutcomeClass.SUCCESS,
                payload=orch._synthesize_trivial_plan(
                    run_id,
                    enriched_description,
                    chunk,
                ),
                evidence=("synthesized_from_triage",),
            )
        else:
            plan_outcome = await plan_stage(
                enriched_description,
                run_id,
                chunk_number=chunk_number,
                project_id=project_id,
                run_planner_fn=orch.run_planner,
            )
        base_stage_outcomes.append(plan_outcome)
        plan = orch._surface_files_expected_for_edit(
            plan_outcome.payload, chunk.files_expected
        )

    recovery_attempts = None
    auto_retry_base: PatchFailureReport | None = None
    auto_retries_spent = 0
    attempt_entry_mode = original_entry_mode

    while True:
        stage_outcomes = list(base_stage_outcomes)
        code_outcome = await code_stage(
            plan,
            run_id,
            chunk_number=chunk_number,
            project_id=project_id,
            files_expected=chunk.files_expected,
            run_coder_fn=orch.run_coder,
            continuation_context=continuation_context,
        )
        stage_outcomes.append(code_outcome)
        code = code_outcome.payload
        if code_outcome.failure is not None:
            _record_attempt(
                run_id=run_id,
                project_id=project_id,
                chunk_number=chunk_number,
                entry_mode=attempt_entry_mode,
                stage_outcomes=stage_outcomes,
                final_outcome_class=code_outcome.outcome_class,
                final_status="failed",
                target_repo_path=target_repo_path,
                stage_profile=ledger_stage_profile,
                trivial_profile_eligible=ledger_trivial_profile_eligible,
            )
            return _finalize_failed_attempt(
                run_id,
                chunk_number,
                code_outcome.failure,
                code,
                auto_retry_base,
                mode=mode,
                prior_attempts=prior_attempts,
                refinement=refinement,
                target_repo_path=target_repo_path,
            )

        preflight_outcome = preflight_stage(
            code,
            files_expected=chunk.files_expected,
            target_repo_path=target_repo_path,
            chunk_number=chunk_number,
            dry_run_fn=orch.dry_run_changes,
        )
        stage_outcomes.append(preflight_outcome)
        if preflight_outcome.failure is not None:
            _record_attempt(
                run_id=run_id,
                project_id=project_id,
                chunk_number=chunk_number,
                entry_mode=attempt_entry_mode,
                stage_outcomes=stage_outcomes,
                final_outcome_class=preflight_outcome.outcome_class,
                final_status="failed",
                target_repo_path=target_repo_path,
                stage_profile=ledger_stage_profile,
                trivial_profile_eligible=ledger_trivial_profile_eligible,
            )
            return _finalize_failed_attempt(
                run_id,
                chunk_number,
                preflight_outcome.failure,
                code,
                auto_retry_base,
                mode=mode,
                prior_attempts=prior_attempts,
                refinement=refinement,
                target_repo_path=target_repo_path,
            )

        apply_outcome = await asyncio.to_thread(
            apply_stage,
            code,
            run_id,
            chunk_number=chunk_number,
            files_expected=chunk.files_expected,
            apply_fn=orch.apply_patch_guarded,
        )
        stage_outcomes.append(apply_outcome)
        if apply_outcome.failure is not None:
            _record_attempt(
                run_id=run_id,
                project_id=project_id,
                chunk_number=chunk_number,
                entry_mode=attempt_entry_mode,
                stage_outcomes=stage_outcomes,
                final_outcome_class=apply_outcome.outcome_class,
                final_status="failed",
                target_repo_path=target_repo_path,
                stage_profile=ledger_stage_profile,
                trivial_profile_eligible=ledger_trivial_profile_eligible,
            )
            return _finalize_failed_attempt(
                run_id,
                chunk_number,
                apply_outcome.failure,
                code,
                auto_retry_base,
                mode=mode,
                prior_attempts=prior_attempts,
                refinement=refinement,
                target_repo_path=target_repo_path,
            )
        patch = apply_outcome.payload.patch_result

        test_result = await asyncio.to_thread(
            run_tests_with_rollback,
            patch,
            run_id,
            chunk_number,
            orch._run_tests_baseline_kwargs(verification_baseline),
        )
        orch._persist_test_run_verdict(run_id, chunk_number, test_result)

        verify_outcome = verify_outcome_from_result(
            test_result,
            code,
            chunk,
            target_repo_path=target_repo_path,
            attempts=auto_retries_spent,
            max_attempts=AUTO_RETRY_INFRA_BUDGET,
        )
        stage_outcomes.append(verify_outcome)
        if verify_outcome.failure is not None:
            report = verify_outcome.failure
            if (
                mode not in _HUMAN_ATTEMPT_MODES
                and auto_retry_base is None
                and orch._should_auto_retry_harness_error(
                    report.failure_type,
                    verify_outcome.integrity,
                    auto_retries_spent=auto_retries_spent,
                )
                and report.working_tree_clean
            ):
                _record_attempt(
                    run_id=run_id,
                    project_id=project_id,
                    chunk_number=chunk_number,
                    entry_mode=attempt_entry_mode,
                    stage_outcomes=stage_outcomes,
                    final_outcome_class=verify_outcome.outcome_class,
                    final_status="auto_retrying",
                    target_repo_path=target_repo_path,
                    stage_profile=ledger_stage_profile,
                    trivial_profile_eligible=ledger_trivial_profile_eligible,
                    evidence_refs=["auto_retry_scheduled"],
                )
                auto_retry_base = orch._record_auto_retry_start(report)
                auto_retries_spent += 1
                attempt_entry_mode = EntryMode.AUTO_RETRY
                base_stage_outcomes = []
                continue
            _record_attempt(
                run_id=run_id,
                project_id=project_id,
                chunk_number=chunk_number,
                entry_mode=attempt_entry_mode,
                stage_outcomes=stage_outcomes,
                final_outcome_class=verify_outcome.outcome_class,
                final_status="failed",
                target_repo_path=target_repo_path,
                stage_profile=ledger_stage_profile,
                trivial_profile_eligible=ledger_trivial_profile_eligible,
            )
            return _finalize_failed_attempt(
                run_id,
                chunk_number,
                report,
                code,
                auto_retry_base,
                mode=mode,
                prior_attempts=prior_attempts,
                refinement=refinement,
                target_repo_path=target_repo_path,
            )

        if auto_retry_base is not None:
            recovered = orch._record_auto_retry_result(
                auto_retry_base,
                auto_retry_base,
                outcome="recovered",
                code=code,
            )
            recovery_attempts = recovered.attempts
        break

    verification_disclosure = orch._verification_disclosure_from_result(
        verification_baseline,
        test_result,
    )
    review_outcome = await review_stage(
        run_id,
        project_id,
        chunk,
        code,
        patch,
        test_result,
        run_review_fn=orch.run_chunk_review,
    )
    stage_outcomes.append(review_outcome)

    if mode in _HUMAN_ATTEMPT_MODES:
        if refinement is not None:
            # Post-success refinement (item 14): there is no failure report to
            # carry, and the pause marker must record restore context, so this
            # uses the sibling pause that re-opens the chunk gate over an already
            # approved one. The new commit lands only on chunk re-approval (D1).
            pause = orch._pause_refined_chunk(
                run_id,
                chunk,
                code,
                branch_name,
                refinement,
                verification_disclosure=verification_disclosure,
            )
        else:
            pause = orch._pause_recovered_chunk(
                run_id,
                chunk,
                code,
                branch_name,
                retry_report,
                verification_disclosure=verification_disclosure,
                recovery_mode=_RECOVERY_MODE_BY_ENTRY_MODE[mode],
            )
        stage_outcomes.append(_gate_outcome(OutcomeClass.NEEDS_HUMAN))
        _record_attempt(
            run_id=run_id,
            project_id=project_id,
            chunk_number=chunk_number,
            entry_mode=attempt_entry_mode,
            stage_outcomes=stage_outcomes,
            final_outcome_class=OutcomeClass.NEEDS_HUMAN,
            final_status="awaiting_chunk_approval",
            target_repo_path=target_repo_path,
            stage_profile=ledger_stage_profile,
            trivial_profile_eligible=ledger_trivial_profile_eligible,
        )
        return pause

    if chunk.requires_human_review:
        pause = orch._pause_for_chunk_approval(
            run_id,
            chunk,
            code,
            branch_name,
            verification_disclosure=verification_disclosure,
        )
        stage_outcomes.append(_gate_outcome(OutcomeClass.NEEDS_HUMAN))
        _record_attempt(
            run_id=run_id,
            project_id=project_id,
            chunk_number=chunk_number,
            entry_mode=attempt_entry_mode,
            stage_outcomes=stage_outcomes,
            final_outcome_class=OutcomeClass.NEEDS_HUMAN,
            final_status="awaiting_chunk_approval",
            target_repo_path=target_repo_path,
            stage_profile=ledger_stage_profile,
            trivial_profile_eligible=ledger_trivial_profile_eligible,
        )
        return pause

    committed_head = orch._commit_and_complete_chunk(
        run_id,
        chunk,
        code,
        target_repo_path,
        project_id,
        plan,
        recovery_attempts=recovery_attempts,
        verification_disclosure=verification_disclosure,
    )
    stage_outcomes.append(_gate_outcome(OutcomeClass.SUCCESS))
    _record_attempt(
        run_id=run_id,
        project_id=project_id,
        chunk_number=chunk_number,
        entry_mode=attempt_entry_mode,
        stage_outcomes=stage_outcomes,
        final_outcome_class=OutcomeClass.SUCCESS,
        final_status="completed",
        target_repo_path=target_repo_path,
        stage_profile=ledger_stage_profile,
        trivial_profile_eligible=ledger_trivial_profile_eligible,
        head_sha=committed_head,
    )
    orch._roll_verification_baseline_forward(
        run_id, verification_baseline, test_result
    )
    return None


async def drive_chunk(
    mode: EntryMode,
    *,
    run_id: str,
    project_id: str,
    chunk: ChunkDefinition,
    target_repo_path: str,
    branch_name: str,
    status_by_number: dict[int, str],
    verification_baseline: dict | None = None,
    chunk_status: ChunkStatus | None = None,
    retry_report: PatchFailureReport | None = None,
    prior_attempts: list | None = None,
    project_runtime=None,
    run_step: str | None = None,
    continuation_context: str | None = None,
    refinement: RefinementContext | None = None,
    stage_profile: StageProfile | None = None,
    trivial_profile_eligible: bool | None = None,
) -> ChunkDriveResult:
    """
    Execute one driver pass over one chunk in the given entry mode.

    fresh:   all stages, top to bottom.
    resume:  skip-complete on a verified test checkpoint (fail closed on an
             unverifiable one — raises, never skips); otherwise all stages.
    steered: human_retry plus the caller-built continuation context (approved
             plan + prior handoff + prior diff as text + failure evidence +
             the steer), threaded into the code stage as advisory prompt
             context only. Same gates, rollback, and budget as human_retry.
             With ``refinement`` set it is a post-success steer of a COMPLETED
             chunk (item 14): same machinery, but failures restore-to-completed
             (§0) and the pause uses the refinement marker.

    Any exception from the stage sequence is converted to the standard failed
    chunk result, exactly as the orchestrator's per-chunk guard did — EXCEPT for
    a refinement, where it restores the completed chunk (§0). The resume
    checkpoint phase is deliberately OUTSIDE that guard: an unsafe resume
    recovery must keep raising out of the resume call, not soften into a
    per-chunk failure.
    """
    if mode is EntryMode.AUTO_RETRY:
        raise ValueError(
            "chunk_driver.py: auto_retry is internal to the driver loop; "
            "enter via fresh or resume."
        )
    if refinement is not None and mode is not EntryMode.STEERED:
        raise ValueError(
            "chunk_driver.py: a refinement context is only valid for a steered "
            "pass (post-success refinement)."
        )
    if mode in _HUMAN_ATTEMPT_MODES:
        # A refinement carries no failure report (a completed chunk has none);
        # the RefinementContext is its reason to exist instead.
        if retry_report is None and refinement is None:
            raise ValueError(
                f"chunk_driver.py: {mode.value} mode requires retry_report."
            )
        if project_runtime is None:
            raise ValueError(
                f"chunk_driver.py: {mode.value} mode requires project_runtime."
            )
    if mode is EntryMode.STEERED and not continuation_context:
        raise ValueError(
            "chunk_driver.py: steered mode requires the continuation context "
            "(the steer is its reason to exist)."
        )
    active_stage_profile = stage_profile if mode is EntryMode.FRESH else None
    active_trivial_profile_eligible = (
        trivial_profile_eligible if mode is EntryMode.FRESH else None
    )

    if mode is EntryMode.RESUME:
        if chunk_status is None:
            raise ValueError(
                "chunk_driver.py: resume mode requires the chunk_status row "
                "(its completion_summary backs the checkpoint verification)."
            )
        skip = _resume_skip_or_none(
            run_id, chunk, chunk_status, target_repo_path, status_by_number
        )
        if skip is not None:
            final_outcome = (
                OutcomeClass.POLICY_BLOCKED
                if skip.pause is not None
                else OutcomeClass.SUCCESS
            )
            final_status = "failed" if skip.pause is not None else "skipped"
            _record_attempt(
                run_id=run_id,
                project_id=project_id,
                chunk_number=chunk.chunk_number,
                entry_mode=mode,
                stage_outcomes=[
                    StageOutcome(
                        stage="resume_checkpoint",
                        outcome_class=final_outcome,
                    )
                ],
                final_outcome_class=final_outcome,
                final_status=final_status,
                target_repo_path=target_repo_path,
            )
            return skip

    async def _run_drive() -> ChunkDriveResult:
        try:
            pause = await _drive_stages(
                mode,
                run_id,
                project_id,
                chunk,
                target_repo_path,
                branch_name,
                status_by_number,
                verification_baseline,
                retry_report=retry_report,
                prior_attempts=prior_attempts,
                run_step=run_step,
                continuation_context=continuation_context,
                refinement=refinement,
                stage_profile=active_stage_profile,
                trivial_profile_eligible=active_trivial_profile_eligible,
            )
        except Exception as error:
            _record_attempt(
                run_id=run_id,
                project_id=project_id,
                chunk_number=chunk.chunk_number,
                entry_mode=mode,
                stage_outcomes=[],
                final_outcome_class=OutcomeClass.INFRA_ERROR,
                final_status="failed",
                target_repo_path=target_repo_path,
                stage_profile=active_stage_profile,
                trivial_profile_eligible=active_trivial_profile_eligible,
                evidence_refs=[f"exception={type(error).__name__}"],
            )
            # §0: an unexpected error during a refinement must not mark the
            # completed chunk failed — restore it instead. Every other mode keeps
            # the standard failed-chunk conversion.
            if refinement is not None:
                return ChunkDriveResult(
                    pause=_orch()._finalize_failed_refinement(
                        run_id,
                        chunk.chunk_number,
                        refinement=refinement,
                        target_repo_path=target_repo_path,
                        message=str(error),
                    )
                )
            return ChunkDriveResult(
                pause=_orch()._fail_chunk(run_id, chunk.chunk_number, error)
            )
        return ChunkDriveResult(pause=pause)

    if mode in _HUMAN_ATTEMPT_MODES:
        with _orch().active_project(project_runtime):
            return await _run_drive()
    return await _run_drive()
