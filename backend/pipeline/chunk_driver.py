"""
chunk_driver.py
Phase 2 item 11 — the one driver for chunk execution (proposal §4.1).

Collapses the per-chunk stage sequence that previously existed in three copies
(`_execute_single_chunk`, its inline auto-retry, and `_execute_retry_attempt`)
into one loop over the item-10 stage contract. In this slice the driver serves
exactly two entry modes — `fresh` and `resume` — plus the bounded INFRA
auto-retry internal to the loop. `human_retry` and `steered` are named seams
only (item 12 / Phase 3); dispatch refuses them loudly.

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
from backend.pipeline.patch_failures import (
    PatchFailureReport,
    PatchFailureType,
    build_patch_failure_report,
)
from backend.pipeline.policy import AUTO_RETRY_INFRA_BUDGET
from backend.pipeline.stage_contract import (
    apply_stage,
    code_stage,
    plan_stage,
    preflight_stage,
    review_stage,
    verify_outcome_from_result,
)

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
    # Named seams only — item 12 (human_retry) and Phase 3 (steered) plug in
    # here without reshaping the driver. Dispatch refuses them until then.
    HUMAN_RETRY = "human_retry"
    STEERED = "steered"


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
) -> dict:
    """
    Persist a failed attempt and mark the chunk failed.

    On the auto-retry attempt the report is first folded into the carried
    attempt history (recovery_mode="auto"), exactly as the deleted inline
    retry did at each of its failure sites.
    """
    orch = _orch()
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
    run_id: str,
    project_id: str,
    chunk: ChunkDefinition,
    target_repo_path: str,
    branch_name: str,
    status_by_number: dict[int, str],
    verification_baseline: dict | None,
) -> dict | None:
    """
    The ordered stage sequence for one chunk:
    plan → code → preflight → apply → verify → review → gate-or-commit,
    with the bounded INFRA auto-retry re-entering at the code stage.

    Returns the pause/failure dict, or None when the chunk completed.
    """
    orch = _orch()
    chunk_number = chunk.chunk_number

    # Dependency-execution guard (#24A): a chunk may only run once every chunk
    # in its depends_on is completed. This runs BEFORE the chunk is marked
    # running and before any planner/coder/patch/test work.
    unmet = orch._unmet_dependencies(chunk, status_by_number)
    if unmet:
        return orch._fail_chunk(
            run_id,
            chunk_number,
            orch._dependency_not_met_message(chunk_number, unmet, status_by_number),
        )

    orch.update_chunk_status(run_id, chunk_number, "running")
    orch._update_run_status(
        run_id,
        "running_chunks",
        f"chunk_{chunk_number}",
        chunk_number,
    )

    # Clean-tree precondition (#18D): fail fast before any planner/coder/patch
    # work if the target git repo already has uncommitted changes, so a later
    # rollback can never clobber unsaved work.
    if not local_git.is_working_tree_clean(target_repo_path):
        report = build_patch_failure_report(
            PatchFailureType.DIRTY_WORKTREE,
            allowed_files=chunk.files_expected,
            working_tree_clean=False,
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return orch._fail_chunk_with_report(run_id, chunk_number, report)

    enriched_description = orch._build_enriched_feature_description(
        run_id,
        project_id,
        chunk,
    )
    plan_outcome = await plan_stage(
        enriched_description,
        run_id,
        chunk_number=chunk_number,
        project_id=project_id,
        run_planner_fn=orch.run_planner,
    )
    # E8 symmetry with the retry path: surface the approved files_expected to
    # the coder via files_to_modify. Prompt context only — it grants NO write
    # authority; the write scope stays files_expected, enforced by scope_guard
    # and apply_patch_guarded.
    plan = orch._surface_files_expected_for_edit(
        plan_outcome.payload, chunk.files_expected
    )

    recovery_attempts = None
    # Auto-retry bookkeeping: auto_retry_base is the failed first attempt's
    # report enriched with its initial attempt record; non-None means the loop
    # is on its (single, budgeted) auto-retry pass.
    auto_retry_base: PatchFailureReport | None = None
    auto_retries_spent = 0

    while True:
        code_outcome = await code_stage(
            plan,
            run_id,
            chunk_number=chunk_number,
            project_id=project_id,
            files_expected=chunk.files_expected,
            run_coder_fn=orch.run_coder,
        )
        code = code_outcome.payload
        if code_outcome.failure is not None:
            return _finalize_failed_attempt(
                run_id, chunk_number, code_outcome.failure, code, auto_retry_base
            )

        # Preflight: scope pre-check then zero-mutation dry-run (#26B), before
        # any write, in every mode. scope_guard stays the authority.
        preflight_outcome = preflight_stage(
            code,
            files_expected=chunk.files_expected,
            target_repo_path=target_repo_path,
            chunk_number=chunk_number,
            dry_run_fn=orch.dry_run_changes,
        )
        if preflight_outcome.failure is not None:
            return _finalize_failed_attempt(
                run_id,
                chunk_number,
                preflight_outcome.failure,
                code,
                auto_retry_base,
            )

        # #32C: offload the blocking patch-apply and test subprocess work to a
        # worker thread so the asyncio event loop (API/UI) stays responsive.
        # `await` keeps the project repo lock held; contextvars propagate.
        apply_outcome = await asyncio.to_thread(
            apply_stage,
            code,
            run_id,
            chunk_number=chunk_number,
            files_expected=chunk.files_expected,
            apply_fn=orch.apply_patch_guarded,
        )
        if apply_outcome.failure is not None:
            return _finalize_failed_attempt(
                run_id, chunk_number, apply_outcome.failure, code, auto_retry_base
            )
        patch = apply_outcome.payload.patch_result

        # Verify: run tests on a worker thread; the driver-owned rollback (T2)
        # runs on that same thread, before the verdict/report read the tree.
        test_result = await asyncio.to_thread(
            run_tests_with_rollback,
            patch,
            run_id,
            chunk_number,
            orch._run_tests_baseline_kwargs(verification_baseline),
        )

        # #28D: record the display-only runtime test verdict for BOTH pass and
        # fail, before any branch decision.
        orch._persist_test_run_verdict(run_id, chunk_number, test_result)

        verify_outcome = verify_outcome_from_result(
            test_result,
            code,
            chunk,
            target_repo_path=target_repo_path,
            attempts=auto_retries_spent,
            max_attempts=AUTO_RETRY_INFRA_BUDGET,
        )
        if verify_outcome.failure is not None:
            report = verify_outcome.failure
            # Item 8's auto-retry rule, in one place: only a verify-stage
            # HARNESS_ERROR with non-TIMEOUT integrity, within budget, on a
            # clean (rolled-back) tree. Never keyed on OutcomeClass.INFRA_ERROR
            # (brief §11.2a: apply-phase UNKNOWN_PATCH_FAILURE must not retry).
            if (
                auto_retry_base is None
                and orch._should_auto_retry_harness_error(
                    report.failure_type,
                    verify_outcome.integrity,
                    auto_retries_spent=auto_retries_spent,
                )
                and report.working_tree_clean
            ):
                auto_retry_base = orch._record_auto_retry_start(report)
                auto_retries_spent += 1
                continue
            return _finalize_failed_attempt(
                run_id, chunk_number, report, code, auto_retry_base
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

    # Adversarial Reviewer v1 (advisory, display-only): best-effort evidence
    # only — never changes the outcome, never gates, fully swallowed. Not run
    # on failed attempts (all returned above).
    await review_stage(
        run_id,
        project_id,
        chunk,
        code,
        patch,
        test_result,
        run_review_fn=orch.run_chunk_review,
    )

    if chunk.requires_human_review:
        return orch._pause_for_chunk_approval(
            run_id,
            chunk,
            code,
            branch_name,
            verification_disclosure=verification_disclosure,
        )

    orch._commit_and_complete_chunk(
        run_id,
        chunk,
        code,
        target_repo_path,
        project_id,
        plan,
        recovery_attempts=recovery_attempts,
        verification_disclosure=verification_disclosure,
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
) -> ChunkDriveResult:
    """
    Execute one driver pass over one chunk in the given entry mode.

    fresh:  all stages, top to bottom.
    resume: skip-complete on a verified test checkpoint (fail closed on an
            unverifiable one — raises, never skips); otherwise all stages.

    Any exception from the stage sequence is converted to the standard failed
    chunk result, exactly as the orchestrator's per-chunk guard did. The
    resume checkpoint phase is deliberately OUTSIDE that guard: an unsafe
    resume recovery must keep raising out of the resume call, not soften into
    a per-chunk failure.
    """
    if mode in (EntryMode.HUMAN_RETRY, EntryMode.STEERED):
        raise NotImplementedError(
            f"chunk_driver.py: entry mode '{mode.value}' is not implemented "
            "in Phase 2 item 11 (human_retry is item 12; steered is Phase 3)."
        )
    if mode is EntryMode.AUTO_RETRY:
        raise ValueError(
            "chunk_driver.py: auto_retry is internal to the driver loop; "
            "enter via fresh or resume."
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
            return skip

    try:
        pause = await _drive_stages(
            run_id,
            project_id,
            chunk,
            target_repo_path,
            branch_name,
            status_by_number,
            verification_baseline,
        )
    except Exception as error:
        return ChunkDriveResult(pause=_orch()._fail_chunk(run_id, chunk.chunk_number, error))
    return ChunkDriveResult(pause=pause)
