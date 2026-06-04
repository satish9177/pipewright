"""
chunked_orchestrator.py
Phase 2B-4 chunk execution for approved chunk plans.

This module executes approved chunks one at a time. It does not implement
remote push, GitHub PR creation, or remote branch management.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from backend.core.status_service import (
    publish_chunk_status_changed as _publish_chunk_status_changed,
    publish_run_status_changed as _publish_run_status_changed,
    update_chunk_status as _service_update_chunk_status,
    update_run_status as _service_update_run_status,
)
from backend.core.statuses import ChunkStatusValue, RunStatus
from backend.db.database import engine, init_db
from backend.events import event_bus
from backend.events.schema import Event
from backend.git import local_git
from backend.checkpoint.checkpoint_store import load_chunk_step_checkpoint
from backend.models.chunk import ChunkDefinition, ChunkPlanResponse, ChunkStatus
from backend.models.handoff import CoderHandoff, PlannerHandoff
from backend.pipeline.approval_gate import (
    create_chunk_approval_gate_and_mark_chunk,
    create_final_approval_gate_and_mark_run,
    create_memory_conflict_gate_and_mark_run,
    get_approved_memory_conflict_gate,
)
from backend.pipeline.chunk_store import (
    get_chunk_plan_status,
    get_previous_chunks_context,
    save_chunk_completion_summary,
    save_chunk_test_run_verdict,
)
from backend.memory.conflict_scope import is_db_sensitive_run
from backend.memory.memory_store import mark_fact_stale
from backend.memory.repo_reality import evaluate_db_memory_conflicts
from backend.pipeline.coder import run_coder
from backend.pipeline.patch_applier import (
    apply_patch_guarded,
    classify_patch_failure,
    rollback_patch,
)
from backend.pipeline.patch_dry_run import dry_run_changes
from backend.pipeline.patch_failures import (
    PatchFailureReport,
    PatchFailureType,
    RETRY_INELIGIBLE_WRONG_BRANCH,
    RecoveredPatchReviewSummary,
    build_patch_failure_report,
    evaluate_patch_retry_eligibility,
    patch_failure_report_from_completion_summary,
    patch_failure_report_to_completion_summary,
    record_initial_attempt,
    record_retry_attempt,
    recovered_patch_review_to_completion_summary,
)
from backend.pipeline.planner import run_planner
from backend.pipeline.run_locks import project_repo_lock, project_repo_lock_sync
from backend.pipeline.scope_expansion import (
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_CHUNK_NOT_FAILED,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_DIRTY_WORKTREE,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_MISSING_REPORT,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_PLAN_NOT_APPROVED,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_STALE_REPORT,
    ScopeExpansionStatus,
    ScopeExpansionValidationError,
    evaluate_scope_expansion_approve_retry_eligibility,
    validate_approved_files,
)
from backend.pipeline.scope_expansion_store import (
    ScopeExpansionConflictError,
    count_in_force_scope_amendments,
    get_scope_expansion_request,
    list_scope_expansion_requests_for_chunk,
    maybe_create_scope_expansion_request_for_failure,
    update_scope_expansion_request_status,
)
from backend.pipeline.scope_guard import ScopeDriftError, assert_files_in_scope
from backend.pipeline.test_run_validation import classify_test_run
from backend.pipeline.tester import run_tests
from backend.projects.project_context import (
    ProjectRuntimeConfig,
    active_project,
    get_test_command,
)
from backend.projects.project_store import require_project
from backend.repo.repo_indexer import build_repo_index, get_relevant_files
from backend.utils.path_safety import normalize_relative_path

logger = logging.getLogger(__name__)
NO_CHANGES_MESSAGE = "Coder produced no file changes."
NO_EFFECTIVE_CHANGES_MESSAGE = (
    "Patch produced no effective changes (working tree clean). "
    "The requested change may already be present; nothing was committed."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _publish_safe(event) -> None:
    try:
        event_bus.publish(event)
    except Exception as error:
        logger.warning("[EVENT_BUS] publish raised, ignored: %s", error)


def _collect_files_expected(chunks) -> list[str]:
    files: list[str] = []
    for chunk in chunks:
        files.extend(getattr(chunk, "files_expected", None) or [])
    return files


_DB_DISPLAY_NAMES = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "sqlite": "SQLite",
}


def _db_display(value: str | None) -> str:
    if not value:
        return "unknown"
    return _DB_DISPLAY_NAMES.get(value, value)


def _conflict_signature(report) -> str:
    """
    Stable, deterministic fingerprint of the current DB conflict. Used to scope an
    approved override to the exact conflict the human saw — a changed conflict must
    re-block. Pure.
    """
    memory_values = sorted({entry.memory_value for entry in report.conflicts})
    return f"{report.repo_db_signal}|{','.join(memory_values)}"


def _build_memory_conflict_summary(report) -> str:
    """
    Build the human-facing block message (gate ai_summary, §7 of the design doc).
    Pure. Evidence is the evaluator's path + fixed excerpt only — never secrets.
    """
    memory_values = sorted({entry.memory_value for entry in report.conflicts})
    memory_display = ", ".join(_db_display(value) for value in memory_values)
    repo_display = _db_display(report.repo_db_signal)
    evidence_path = report.conflicts[0].evidence_path or "repo manifest"
    return (
        "Pipewright found a DB memory conflict relevant to this run.\n"
        f"Memory says: {memory_display}\n"
        f"Repo evidence says: {repo_display} (from {evidence_path})\n"
        "This run appears to modify DB/model/migration files.\n"
        "Resolve the stale memory or override once to continue."
    )


def _stale_db_conflict_facts(project_id: str, report) -> None:
    """
    Mark clearly conflicting DB memory stale before pausing on the gate.

    The evaluator remains read-only; this mutation is tied to the blocking gate path
    so an approved override cannot inject the stale DB fact into planner/coder prompts.
    """
    for entry in report.conflicts:
        mark_fact_stale(
            project_id=project_id,
            memory_id=entry.fact_id,
            reason=(
                "repo reality conflict: "
                f"repo={entry.repo_value}, memory={entry.memory_value}"
            ),
        )


def _db_conflict_block_decision(
    report,
    files_expected: list[str],
    approved_signature: str | None,
) -> tuple[str, str] | None:
    """
    Pure blocking-policy decision (no I/O). Returns ``(summary, signature)`` when the
    run must block on a DB memory conflict, else ``None``.

    Blocks only when ALL hold: a clear conflict exists, the repo DB signal is known and
    not ambiguous, the run is DB-sensitive, and no approved override matches the current
    conflict. Any other case returns ``None`` (proceed / warn).
    """
    if not report.conflicts or report.ambiguous or not report.repo_db_signal:
        return None
    if not is_db_sensitive_run(files_expected):
        return None
    signature = _conflict_signature(report)
    if approved_signature is not None and approved_signature == signature:
        return None
    return (_build_memory_conflict_summary(report), signature)


def _apply_db_memory_conflict_policy(
    run_id: str,
    project_id: str,
    repo_path: str,
    files_expected: list[str],
) -> dict | None:
    """
    Run the DB memory-conflict policy once, before any branch/patch/commit (#16D-4).

    Evaluates DB memory vs. repo reality with a read-only comparison. On a clear
    conflict for a DB-sensitive run with no matching approved override, it marks the
    conflicting DB memory facts stale, creates a blocking ``memory_conflict`` gate,
    pauses the run, and returns a pause result. Otherwise it preserves the #16D-3
    non-blocking warning and returns ``None``.
    """
    try:
        report = evaluate_db_memory_conflicts(project_id, repo_path)
    except Exception as error:
        # Evaluation problems must never silently corrupt a run; fall back to the
        # safe, non-blocking path (no conflict info to surface).
        logger.warning(
            "[CHUNKED] db conflict evaluation failed, not blocking | run_id=%s | error=%s",
            run_id, error,
        )
        return None

    approved = get_approved_memory_conflict_gate(run_id)
    approved_signature = (approved.get("test_results") or "") if approved else None

    decision = _db_conflict_block_decision(report, files_expected, approved_signature)
    if decision is not None:
        summary, signature = decision
        _stale_db_conflict_facts(project_id, report)
        gate = create_memory_conflict_gate_and_mark_run(run_id, summary, signature)
        logger.info(
            "[CHUNKED] DB memory conflict gate engaged | run_id=%s | gate_id=%s",
            run_id, gate["id"],
        )
        return {
            "status": "awaiting_memory_conflict_approval",
            "run_id": run_id,
            "approval_required": True,
            "gate_id": gate["id"],
        }

    # Not blocking: preserve the #16D-3 warning, reusing the single evaluation.
    _emit_db_conflict_warning(
        run_id, project_id, repo_path, files_expected, report=report
    )
    return None


def _emit_db_conflict_warning(
    run_id: str,
    project_id: str,
    repo_path: str,
    files_expected: list[str],
    report=None,
) -> None:
    """
    Non-blocking notice (#16D-3). Evaluates DB memory vs. repo reality READ-ONLY and,
    on a conflict, emits a single run log event. It never blocks, pauses, changes run
    status, marks memory stale, or creates an approval gate. Must never raise.

    Surface: a `log`-kind event on the existing event bus (visible in run detail's
    live event stream). Severity is `warning` for DB-sensitive runs, else `info`.

    ``report`` may be a precomputed ConflictReport to avoid re-evaluating (so the gate
    policy and this warning together evaluate at most once per run).
    """
    try:
        if report is None:
            report = evaluate_db_memory_conflicts(project_id, repo_path)
        if not report.conflicts and not report.ambiguous:
            return

        sensitive = is_db_sensitive_run(files_expected)

        if report.conflicts:
            level = "warning" if sensitive else "info"
            repo_value = report.conflicts[0].repo_value
            evidence_path = report.conflicts[0].evidence_path or "repo manifest"
            memory_values = sorted({entry.memory_value for entry in report.conflicts})
            if sensitive:
                message = (
                    f"DB memory conflict relevant to this run: memory says "
                    f"{', '.join(memory_values)}, repo says {repo_value} "
                    f"({evidence_path}). The run was NOT blocked; conflicting memory "
                    f"is excluded from prompts. Resolve via memory verify/update/archive."
                )
            else:
                message = (
                    f"DB memory conflict detected (memory: {', '.join(memory_values)}, "
                    f"repo: {repo_value} from {evidence_path}) but this run does not "
                    f"appear to touch DB files, so it was not blocked."
                )
            data = {
                "type": "memory_db_conflict",
                "repo_db_signal": report.repo_db_signal,
                "db_sensitive": sensitive,
                "conflicts": [
                    {
                        "fact_id": entry.fact_id,
                        "memory_value": entry.memory_value,
                        "repo_value": entry.repo_value,
                        "evidence_path": entry.evidence_path,
                        "evidence_excerpt": entry.evidence_excerpt,
                    }
                    for entry in report.conflicts
                ],
            }
        elif report.ambiguous and sensitive:
            level = "info"
            message = (
                "Repository DB signal is ambiguous (multiple engines detected); "
                "DB memory was not evaluated for conflicts. The run was not blocked."
            )
            data = {"type": "memory_db_conflict_ambiguous"}
        else:
            return

        _publish_safe(Event(
            run_id=run_id,
            kind="log",
            stage="orchestrator",
            level=level,
            message=message,
            data=data,
        ))
        logger.info(
            "[CHUNKED] DB memory conflict notice | run_id=%s | level=%s | sensitive=%s",
            run_id, level, sensitive,
        )
    except Exception as error:
        # A non-blocking warning must never affect execution.
        logger.warning(
            "[CHUNKED] db conflict warning failed, ignored | run_id=%s | error=%s",
            run_id, error,
        )


def update_chunk_status(
    run_id: str,
    chunk_number: int,
    status: str,
    error_message: str | None = None,
) -> None:
    _service_update_chunk_status(
        run_id,
        chunk_number,
        status,
        error_message,
        publish_event=True,
    )


def _update_run_status(
    run_id: str,
    status: str,
    current_step: str,
    current_chunk_number: int | None = None,
) -> None:
    _service_update_run_status(
        run_id,
        status,
        current_step,
        current_chunk_number,
        publish_event=True,
    )


def _pending_chunks(plan: ChunkPlanResponse) -> list[ChunkStatus]:
    return sorted(
        [chunk for chunk in plan.chunks if chunk.status == "pending"],
        key=lambda chunk: chunk.chunk_number,
    )


def _resumable_chunks(plan: ChunkPlanResponse) -> list[ChunkStatus]:
    return sorted(
        [
            chunk for chunk in plan.chunks
            if chunk.status in {"pending", "failed", "running", "rejected"}
        ],
        key=lambda chunk: chunk.chunk_number,
    )


def _has_running_chunk(plan: ChunkPlanResponse) -> bool:
    return any(chunk.status == "running" for chunk in plan.chunks)


def _status_by_number(plan: ChunkPlanResponse) -> dict[int, str]:
    """Snapshot of every chunk's current status keyed by chunk_number."""
    return {chunk.chunk_number: chunk.status for chunk in plan.chunks}


def _unmet_dependencies(
    chunk: ChunkDefinition,
    status_by_number: dict[int, str],
) -> list[int]:
    """
    Return the depends_on chunk numbers that are NOT satisfied.

    A dependency is satisfied only when its current status is exactly
    ``completed``. Any other status (pending/running/failed/rejected/
    awaiting_chunk_approval) — and a missing/unknown chunk number — counts as
    unmet, so the check fails safe.
    """
    return [
        dependency
        for dependency in chunk.depends_on
        if status_by_number.get(dependency) != ChunkStatusValue.COMPLETED
    ]


def _dependency_not_met_message(
    chunk_number: int,
    unmet: list[int],
    status_by_number: dict[int, str],
) -> str:
    """Human-readable DEPENDENCY_NOT_MET error for a blocked chunk."""
    details = ", ".join(
        f"chunk {dependency} status: "
        f"{status_by_number.get(dependency, 'missing')}"
        for dependency in unmet
    )
    return (
        f"DEPENDENCY_NOT_MET: chunk {chunk_number} requires chunks {unmet} "
        f"to be completed first ({details})"
    )


def _awaiting_approval_chunk(plan: ChunkPlanResponse) -> ChunkStatus | None:
    for chunk in sorted(plan.chunks, key=lambda item: item.chunk_number):
        if chunk.status == "awaiting_chunk_approval":
            return chunk
    return None


def _definition_by_number(plan: ChunkPlanResponse) -> dict[int, ChunkDefinition]:
    if plan.triage is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: missing triage plan for run {plan.run_id}"
        )
    definitions = {chunk.chunk_number: chunk for chunk in plan.triage.chunks}
    merged: dict[int, ChunkDefinition] = {}
    for chunk_status in plan.chunks:
        base = definitions.get(chunk_status.chunk_number)
        if base is None:
            raise RuntimeError(
                f"chunked_orchestrator.py: chunk definition missing. "
                f"run_id={plan.run_id} | chunk={chunk_status.chunk_number}"
            )
        merged[chunk_status.chunk_number] = ChunkDefinition(
            chunk_number=chunk_status.chunk_number,
            title=chunk_status.title,
            description=base.description,
            files_expected=chunk_status.files_expected,
            depends_on=chunk_status.depends_on,
            risk_level=chunk_status.risk_level,
            token_estimate=base.token_estimate,
            requires_human_review=chunk_status.requires_human_review,
            rationale=base.rationale,
        )
    return merged


def _format_relevant_files(relevant_files: list[dict]) -> str:
    if not relevant_files:
        return "- No relevant files found in index"

    lines = []
    for file_data in relevant_files:
        path = file_data.get("path", "")
        file_type = file_data.get("file_type", "unknown")
        tokens = file_data.get("token_estimate", 0)
        lines.append(f"- {path} ({file_type}, ~{tokens} tokens)")
    return "\n".join(lines)


def _normalize_previous_context(context: str) -> str:
    if not context or context.strip() == "[Previous Chunks Context]\n\n[End Previous Context]":
        return (
            "[Previous Chunks Context]\n"
            "No previous chunks completed.\n"
            "[End Previous Context]"
        )
    return context


def _build_enriched_feature_description(
    run_id: str,
    project_id: str,
    chunk: ChunkDefinition,
) -> str:
    previous_context = _normalize_previous_context(
        get_previous_chunks_context(run_id, chunk.chunk_number)
    )
    try:
        relevant_files = get_relevant_files(
            project_id,
            chunk.description,
            limit=20,
        )
    except Exception as error:
        print(
            f"[CHUNKED] Warning: relevant file lookup failed. "
            f"run_id={run_id} | chunk={chunk.chunk_number} | error={error}"
        )
        relevant_files = []

    return (
        f"{previous_context}\n\n"
        f"[Current Chunk Task]\n"
        f"{chunk.description}\n\n"
        f"[Known Relevant Files in Project]\n"
        f"{_format_relevant_files(relevant_files)}"
    )


def _files_touched(coder_output: CoderHandoff) -> list[str]:
    return [change.path.replace("\\", "/") for change in coder_output.files_changed]


def _build_completion_summary(
    chunk: ChunkDefinition,
    plan: PlannerHandoff,
    coder_output: CoderHandoff,
) -> dict:
    files_created = []
    files_modified = []
    files_deleted = []
    tests_added_or_updated = []

    for change in coder_output.files_changed:
        path = change.path.replace("\\", "/")
        if change.action == "create":
            files_created.append(path)
        elif change.action in ("modify", "edit"):
            # A targeted edit (PR #12A) changes an existing file in place, so it
            # is a modification, not a create or delete.
            files_modified.append(path)
        elif change.action == "delete":
            files_deleted.append(path)

        lower_path = path.lower()
        if "test" in lower_path or "/tests/" in lower_path:
            tests_added_or_updated.append(path)

    return {
        "chunk_title": chunk.title,
        "chunk_description": chunk.description,
        "files_created": files_created,
        "files_modified": files_modified,
        "files_deleted": files_deleted,
        "key_decisions": plan.steps,
        "tests_added_or_updated": tests_added_or_updated,
        "tests_added": tests_added_or_updated,
        "summary": (
            f"Goal: {plan.goal}\n"
            f"Coder summary: {coder_output.summary}"
        ),
        "suggested_memory_entries": coder_output.suggested_memory_entries,
    }


def _fallback_plan_for_summary(run_id: str, chunk_number: int) -> PlannerHandoff:
    checkpoint = load_chunk_step_checkpoint(run_id, chunk_number, "plan")
    if checkpoint:
        try:
            return PlannerHandoff.model_validate(checkpoint["output"])
        except Exception:
            pass
    return PlannerHandoff(
        run_id=run_id,
        feature_description="",
        goal="Chunk approved from checkpoint.",
        steps=["Human approved tested chunk changes."],
        files_to_create=[],
        files_to_modify=[],
        files_to_read=[],
        out_of_scope=[],
        risks=[],
        suggested_memory_entries=[],
    )


def _load_code_from_checkpoint(run_id: str, chunk_number: int) -> CoderHandoff:
    checkpoint = load_chunk_step_checkpoint(run_id, chunk_number, "code")
    if checkpoint is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: code checkpoint missing. "
            f"run_id={run_id} | chunk={chunk_number}"
        )
    try:
        return CoderHandoff.model_validate(checkpoint["output"])
    except Exception as error:
        raise RuntimeError(
            f"chunked_orchestrator.py: invalid code checkpoint. "
            f"run_id={run_id} | chunk={chunk_number} | error={error}"
        )


def _chunk_approval_summary(
    run_id: str,
    chunk: ChunkDefinition | ChunkStatus,
    coder_output: CoderHandoff,
) -> str:
    files = _files_touched(coder_output)
    title = getattr(chunk, "title", f"Chunk {getattr(chunk, 'chunk_number', '')}")
    chunk_number = getattr(chunk, "chunk_number")
    branch_name = f"pipewright/{run_id[:8]}"
    file_lines = "\n".join(f"- {path}" for path in files) if files else "- none"
    return (
        f"Chunk approval required for run {run_id}\n\n"
        f"Chunk: {chunk_number} - {title}\n"
        f"Branch: {branch_name}\n\n"
        f"Changed files:\n{file_lines}\n\n"
        "Tests have passed. Commit is pending human approval."
    )


def _pause_for_chunk_approval(
    run_id: str,
    chunk: ChunkDefinition,
    coder_output: CoderHandoff,
    branch_name: str,
) -> dict:
    summary = _chunk_approval_summary(run_id, chunk, coder_output)
    create_chunk_approval_gate_and_mark_chunk(
        run_id,
        chunk.chunk_number,
        summary,
    )
    _publish_chunk_status_changed(
        run_id,
        chunk.chunk_number,
        "awaiting_chunk_approval",
    )
    _publish_run_status_changed(
        run_id,
        "awaiting_chunk_approval",
        chunk.chunk_number,
    )
    print(
        f"[CHUNKED] Awaiting chunk approval | "
        f"run_id={run_id} | chunk={chunk.chunk_number}"
    )
    return {
        "status": "awaiting_chunk_approval",
        "run_id": run_id,
        "chunk_number": chunk.chunk_number,
        "approval_required": True,
        "branch_name": branch_name,
    }


def _refresh_index_after_success(
    project_id: str,
    target_repo_path: str,
    *,
    run_id: str,
    chunk_number: int,
) -> None:
    """
    Best-effort post-commit repo index refresh (#19E).

    Refreshes file_index so a later chunk/run sees files Pipewright just
    created/deleted/renamed in this committed chunk. It is called only after a
    chunk has been committed and marked completed.

    This runs inside the project repo lock (every commit path holds it), and the
    lock is non-reentrant, so it calls build_repo_index DIRECTLY — re-acquiring
    the lock would raise. The index is a recoverable cache, so a refresh failure
    must never fail the chunk or run: it is logged and swallowed.
    """
    try:
        build_repo_index(project_id, target_repo_path)
    except Exception as error:
        logger.warning(
            "[CHUNKED] post-commit index refresh failed, ignored | "
            "run_id=%s | chunk=%s | project_id=%s | error=%s",
            run_id,
            chunk_number,
            project_id,
            error,
        )


def _commit_and_complete_chunk(
    run_id: str,
    chunk: ChunkDefinition | ChunkStatus,
    coder_output: CoderHandoff,
    target_repo_path: str,
    project_id: str,
    plan: PlannerHandoff | None = None,
) -> None:
    chunk_number = chunk.chunk_number
    touched_files = _files_touched(coder_output)
    if not touched_files:
        update_chunk_status(run_id, chunk_number, "failed", NO_CHANGES_MESSAGE)
        _update_run_status(run_id, "failed", f"chunk_{chunk_number}_failed", chunk_number)
        raise RuntimeError(NO_CHANGES_MESSAGE)

    # The coder declared file changes, but the patch may have produced no
    # effective on-disk change (e.g. a no-op edit whose result is byte-identical
    # to the original). Committing in that state fails with an opaque git error,
    # so detect it here and fail cleanly before staging. This guard covers both
    # normal non-review chunks and post-approval high-risk chunks, since both
    # commit through this function.
    if local_git.is_working_tree_clean(target_repo_path):
        update_chunk_status(run_id, chunk_number, "failed", NO_EFFECTIVE_CHANGES_MESSAGE)
        _update_run_status(run_id, "failed", f"chunk_{chunk_number}_failed", chunk_number)
        raise RuntimeError(NO_EFFECTIVE_CHANGES_MESSAGE)

    commit_message = f"chunk {chunk_number}: {chunk.title}"
    local_git.commit_files(touched_files, commit_message, target_repo_path)

    summary_plan = plan or _fallback_plan_for_summary(run_id, chunk_number)
    completion_summary = _build_completion_summary(chunk, summary_plan, coder_output)
    save_chunk_completion_summary(run_id, chunk_number, completion_summary)
    update_chunk_status(run_id, chunk_number, "completed")
    print(
        f"[CHUNKED] Chunk complete | "
        f"run_id={run_id} | chunk={chunk_number}"
    )

    # #19E: only after a real, committed, completed chunk — refresh the index so
    # the next chunk/run sees the files this chunk created/deleted/renamed.
    # Best-effort: never fails the run.
    _refresh_index_after_success(
        project_id,
        target_repo_path,
        run_id=run_id,
        chunk_number=chunk_number,
    )


def _build_final_approval_summary(
    run_id: str,
    plan_status: ChunkPlanResponse,
    branch_name: str,
) -> str:
    lines = [
        f"Final approval required for chunked run {run_id}",
        "",
        "Branch:",
        branch_name,
        "",
        "Chunks:",
    ]
    for chunk in plan_status.chunks:
        lines.append(f"{chunk.chunk_number}. {chunk.title} - {chunk.status}")
        if chunk.completion_summary:
            lines.append(f"   Summary: {chunk.completion_summary}")
    lines.extend([
        "",
        "This approval allows the run to proceed to PR creation in a later phase.",
    ])
    return "\n".join(lines)


def _require_all_chunks_completed(plan_status: ChunkPlanResponse) -> None:
    if plan_status.chunk_plan_status != "approved":
        raise RuntimeError(
            f"chunked_orchestrator.py: cannot create final approval; "
            f"chunk_plan_status={plan_status.chunk_plan_status}"
        )
    incomplete = [
        chunk.chunk_number
        for chunk in plan_status.chunks
        if chunk.status != "completed"
    ]
    if incomplete:
        raise RuntimeError(
            f"chunked_orchestrator.py: cannot create final approval; "
            f"incomplete chunks={incomplete}"
        )


def _mark_awaiting_final_approval(
    run_id: str,
    plan_status: ChunkPlanResponse,
    branch_name: str,
    target_repo_path: str | None = None,
) -> dict:
    latest_status = get_chunk_plan_status(run_id)
    _require_all_chunks_completed(latest_status)
    if target_repo_path:
        local_git.ensure_clean_worktree(target_repo_path)
    summary = _build_final_approval_summary(run_id, latest_status, branch_name)
    create_final_approval_gate_and_mark_run(
        run_id,
        summary,
        latest_status.total_chunks,
    )
    _publish_run_status_changed(
        run_id,
        "awaiting_final_approval",
        latest_status.current_chunk_number,
    )
    return {
        "status": "awaiting_final_approval",
        "run_id": run_id,
        "completed_chunks": len(latest_status.chunks),
        "branch_name": branch_name,
        "final_approval_required": True,
    }


def _fail_chunk(
    run_id: str,
    chunk_number: int,
    error,
) -> dict:
    error_text = str(error)
    print(
        f"[CHUNKED] Failed | run_id={run_id} | "
        f"chunk={chunk_number} | error={error_text}"
    )
    update_chunk_status(run_id, chunk_number, "failed", error_text)
    _update_run_status(run_id, "failed", f"chunk_{chunk_number}_failed", chunk_number)
    return {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": chunk_number,
        "error": error_text,
    }


def _surface_scope_expansion_if_eligible(
    run_id: str,
    chunk_number: int,
    report: PatchFailureReport,
) -> None:
    """
    Best-effort: create a pending scope_expansion_request for an eligible clean
    SCOPE_VIOLATION and surface the run as awaiting scope approval (#27).

    The chunk stays `failed` (design §10); only the run-level status is moved to
    AWAITING_SCOPE_APPROVAL so the UI/API can distinguish "scope expansion
    pending" from an ordinary patch failure. This adds NO approval, NO retry, NO
    commit, and never mutates chunks.files_expected or scope_guard.

    Intentionally swallows its own errors: the patch failure has already been
    persisted by the caller, so surfacing must never turn a clean `failed` into a
    crash. A dirty-tree / manual-intervention / non-SCOPE_VIOLATION /
    all-forbidden / cap-exhausted failure creates nothing and leaves the run
    `failed`.
    """
    try:
        project_id = get_chunk_plan_status(run_id).project_id
        result = maybe_create_scope_expansion_request_for_failure(
            run_id, project_id, chunk_number, report
        )
        if result.request is not None:
            _update_run_status(
                run_id,
                RunStatus.AWAITING_SCOPE_APPROVAL,
                f"chunk_{chunk_number}_awaiting_scope_approval",
                chunk_number,
            )
    except Exception as error:
        logger.warning(
            "scope expansion surfacing skipped | run_id=%s | chunk=%s | error=%s",
            run_id,
            chunk_number,
            error,
        )


def settle_run_after_scope_expansion_reject(run_id: str, chunk_number: int) -> None:
    """
    Move the run off AWAITING_SCOPE_APPROVAL back to a plain failed state after a
    scope expansion request was rejected (#27 reject slice).

    The chunk itself stays `failed` (design §10), so dependents remain blocked and
    no execution authority is granted. This only clears the run-level "waiting for
    scope approval" surfacing. It is conservative:

      - it does nothing unless the run is currently AWAITING_SCOPE_APPROVAL (never
        clobbers some other run state);
      - it does nothing if another pending scope request still exists for the
        chunk (still legitimately awaiting a scope decision).

    There is no dedicated manual-intervention run status, so the closest existing
    state — RunStatus.FAILED with the standard `chunk_{n}_failed` step — is used.
    """
    remaining_pending = [
        request
        for request in list_scope_expansion_requests_for_chunk(run_id, chunk_number)
        if request.status == ScopeExpansionStatus.PENDING.value
    ]
    if remaining_pending:
        return

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).fetchone()
    if row is None or row[0] != RunStatus.AWAITING_SCOPE_APPROVAL:
        return

    _update_run_status(
        run_id,
        RunStatus.FAILED,
        f"chunk_{chunk_number}_failed",
        chunk_number,
    )


def _fail_chunk_with_report(
    run_id: str,
    chunk_number: int,
    report: PatchFailureReport,
) -> dict:
    """
    Fail a chunk with a structured PatchFailureReport (#18D).

    Persists the full report JSON into completion_summary, the user-facing
    message into error_message, marks the chunk and run failed, and emits a slim
    stage_failed event. The full technical_details stay in completion_summary;
    the event payload is intentionally small to respect the event-bus size cap.
    """
    print(
        f"[CHUNKED] Patch failure | run_id={run_id} | "
        f"chunk={chunk_number} | type={report.failure_type.value}"
    )
    # #26C: enrich the persisted summary with a failure_report_id and an initial
    # attempt record (diagnostics/idempotency foundation). Ids/timestamp are
    # generated here so build_patch_failure_report stays deterministic. This is
    # additive: the user-facing message, status, event, and all execution
    # behavior are unchanged.
    enriched = record_initial_attempt(
        report,
        failure_report_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        started_at=_utc_now(),
    )
    save_chunk_completion_summary(
        run_id,
        chunk_number,
        patch_failure_report_to_completion_summary(enriched),
    )
    update_chunk_status(run_id, chunk_number, "failed", report.message)
    _update_run_status(
        run_id, "failed", f"chunk_{chunk_number}_failed", chunk_number
    )
    _publish_safe(Event(
        run_id=run_id,
        chunk_number=chunk_number,
        kind="stage_failed",
        stage="patch",
        level="error",
        message=report.message,
        data={
            "kind": "patch_failure",
            "failure_type": report.failure_type.value,
            "chunk_number": chunk_number,
            "failed_step": report.failed_step,
            "rollback_performed": report.rollback_performed,
            "working_tree_clean": report.working_tree_clean,
            "manual_intervention_needed": report.manual_intervention_needed,
            "stale_index_hint": report.stale_index_hint,
            "suggested_actions": report.suggested_actions,
            "changed_files_attempted_count": len(report.changed_files_attempted),
            "changed_files_actual_count": len(report.changed_files_actual),
        },
    ))
    # #27: on an eligible clean SCOPE_VIOLATION, create a pending scope expansion
    # request and surface the run as awaiting scope approval. Uses the enriched
    # report so the request is tied to the persisted failure_report_id. Additive
    # and best-effort; never changes the failed return value below.
    _surface_scope_expansion_if_eligible(run_id, chunk_number, enriched)
    return {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": chunk_number,
        "error": report.message,
    }


def _validate_target_repo(repo_path: str, require_clean: bool = True) -> None:
    target_repo = Path(repo_path)
    if not target_repo.exists() or not target_repo.is_dir():
        raise RuntimeError(
            f"chunked_orchestrator.py: target repo missing: {repo_path}"
        )
    local_git.ensure_git_repo(repo_path)
    if require_clean:
        local_git.ensure_clean_worktree(repo_path)


def _project_runtime_for_plan(plan_status: ChunkPlanResponse) -> tuple[dict, ProjectRuntimeConfig]:
    project = require_project(plan_status.project_id)
    runtime = ProjectRuntimeConfig(
        project_id=project["id"],
        repo_path=project["repo_path"],
        test_command=project["test_command"],
    )
    return project, runtime


def _reset_stale_running_chunks(run_id: str) -> None:
    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE chunks
                SET status = 'pending',
                    error_message = NULL
                WHERE run_id = :run_id
                  AND status = 'running'
            """), {"run_id": run_id})
    except Exception as error:
        raise RuntimeError(
            f"chunked_orchestrator.py: failed to reset stale running chunks. "
            f"run_id={run_id} | error={error}"
        )


def _get_pending_chunk_gate(run_id: str, chunk_number: int) -> dict | None:
    try:
        init_db()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM approval_gates
                WHERE run_id = :run_id
                  AND approval_type = 'chunk'
                  AND chunk_number = :chunk_number
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            }).fetchone()
            return dict(row._mapping) if row else None
    except Exception as error:
        raise RuntimeError(
            f"chunked_orchestrator.py: failed to load chunk gate. "
            f"run_id={run_id} | chunk={chunk_number} | error={error}"
        )


def _decide_pending_chunk_gate(
    run_id: str,
    chunk_number: int,
    status: str,
    reason: str | None = None,
) -> dict:
    try:
        init_db()
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT * FROM approval_gates
                WHERE run_id = :run_id
                  AND approval_type = 'chunk'
                  AND chunk_number = :chunk_number
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            }).fetchone()
            if row is None:
                raise RuntimeError(
                    f"chunked_orchestrator.py: pending chunk gate not found. "
                    f"run_id={run_id} | chunk={chunk_number}"
                )
            gate = dict(row._mapping)
            result = conn.execute(text("""
                UPDATE approval_gates
                SET status = :status,
                    rejection_reason = :reason,
                    decided_at = :decided_at
                WHERE id = :gate_id
                  AND status = 'pending'
            """), {
                "status": status,
                "reason": reason,
                "decided_at": _utc_now(),
                "gate_id": gate["id"],
            })
            if result.rowcount == 0:
                raise RuntimeError(
                    f"chunked_orchestrator.py: chunk gate already decided. "
                    f"run_id={run_id} | chunk={chunk_number}"
                )
            gate["status"] = status
            gate["rejection_reason"] = reason
            return gate
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"chunked_orchestrator.py: failed to decide chunk gate. "
            f"run_id={run_id} | chunk={chunk_number} | error={error}"
        )


def _verify_resume_branch(branch_name: str, repo_path: str) -> None:
    if not local_git.branch_exists(branch_name, repo_path):
        raise RuntimeError(
            f"chunked_orchestrator.py: resume branch missing: {branch_name}. "
            "Manual recovery required."
        )
    result = local_git.run_git(["checkout", branch_name], repo_path)
    if result.returncode != 0:
        raise RuntimeError(
            f"chunked_orchestrator.py: failed to checkout resume branch "
            f"{branch_name}: {result.stderr.strip()}"
        )


def _verify_clean_for_resume(repo_path: str) -> None:
    try:
        local_git.ensure_clean_worktree(repo_path)
    except Exception as error:
        dirty_files = []
        try:
            dirty_files = local_git.get_dirty_files(repo_path)
        except Exception:
            pass
        detail = ", ".join(dirty_files) if dirty_files else str(error)
        raise RuntimeError(
            f"chunked_orchestrator.py: dirty worktree during resume: {detail}. "
            "Manual cleanup or rollback is required."
        )


def _verify_completed_checkpoint_safe(
    run_id: str,
    chunk: ChunkDefinition,
    chunk_status: ChunkStatus,
    repo_path: str,
) -> None:
    commit_prefix = f"chunk {chunk.chunk_number}:"
    if not local_git.commit_message_exists(repo_path, commit_prefix):
        raise RuntimeError(
            f"chunked_orchestrator.py: unsafe resume recovery. "
            f"Test checkpoint exists for chunk {chunk.chunk_number} but "
            f"commit '{commit_prefix}' was not found. Manual intervention required."
        )
    if chunk_status.completion_summary is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: unsafe resume recovery. "
            f"Test checkpoint exists for chunk {chunk.chunk_number} but "
            "completion_summary is missing. Manual intervention required."
        )


def _persist_test_run_verdict(run_id: str, chunk_number: int, test_result) -> None:
    """
    #28D: compute and persist the DISPLAY-ONLY runtime test-validation verdict.

    Joins the configured test command (Signal A) with the run's exit signal and
    output (Signal B) via the pure ``classify_test_run`` classifier, then records
    the verdict on the chunk. This is evidence only: it never gates, blocks,
    commits, rolls back, or changes whether the chunk/run passes. Pass/fail stays
    exit-code based and is decided entirely by ``test_result.passed`` elsewhere.

    Best-effort and fully swallowed on error — a problem recording evidence must
    never fail a chunk or perturb the #26/#27 paths. Called inside the active
    project context so ``get_test_command()`` resolves the same command the tester
    used. ``exit_code`` is derived from ``test_result.passed`` (the tester sets it
    from the real returncode == 0); the classifier only distinguishes zero from
    non-zero, so this is faithful. Output is the #28C tail-preserving preview, so
    a summary at the end of long output is still classifiable.
    """
    try:
        verdict = classify_test_run(
            get_test_command(),
            0 if getattr(test_result, "passed", False) else 1,
            getattr(test_result, "output", None),
        )
        save_chunk_test_run_verdict(run_id, chunk_number, verdict)
    except Exception as error:
        logger.warning(
            "[CHUNKED] test verdict persistence skipped (display-only) | "
            "run_id=%s | chunk=%s | error=%s",
            run_id, chunk_number, error,
        )


async def _execute_single_chunk(
    run_id: str,
    project_id: str,
    chunk: ChunkDefinition,
    target_repo_path: str,
    branch_name: str,
    status_by_number: dict[int, str],
) -> dict | None:
    chunk_number = chunk.chunk_number

    # Dependency-execution guard (#24A): a chunk may only run once every chunk in
    # its depends_on is completed. This runs BEFORE the chunk is marked running
    # and before any planner/coder/patch/test work, so a dependent chunk can
    # never start while a dependency is failed/rejected/pending/awaiting approval
    # (the high-risk-pause re-entry bypass) or missing.
    unmet = _unmet_dependencies(chunk, status_by_number)
    if unmet:
        return _fail_chunk(
            run_id,
            chunk_number,
            _dependency_not_met_message(chunk_number, unmet, status_by_number),
        )

    update_chunk_status(run_id, chunk_number, "running")
    _update_run_status(
        run_id,
        "running_chunks",
        f"chunk_{chunk_number}",
        chunk_number,
    )

    # Clean-tree precondition (#18D): fail fast before any planner/coder/patch
    # work if the target git repo already has uncommitted changes, so a later
    # rollback can never clobber unsaved work. No-op outside a git repo
    # (is_working_tree_clean returns True for non-git paths).
    if not local_git.is_working_tree_clean(target_repo_path):
        report = build_patch_failure_report(
            PatchFailureType.DIRTY_WORKTREE,
            allowed_files=chunk.files_expected,
            working_tree_clean=False,
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return _fail_chunk_with_report(run_id, chunk_number, report)

    enriched_description = _build_enriched_feature_description(
        run_id,
        project_id,
        chunk,
    )
    plan = await run_planner(
        enriched_description,
        run_id,
        chunk_number=chunk_number,
        project_id=project_id,
    )
    code = await run_coder(
        plan,
        run_id,
        chunk_number=chunk_number,
        project_id=project_id,
    )
    if not code.files_changed:
        report = build_patch_failure_report(
            PatchFailureType.NO_CHANGES,
            allowed_files=chunk.files_expected,
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return _fail_chunk_with_report(run_id, chunk_number, report)

    # Pre-apply scope guard (defense-in-depth): catches drift before any write.
    # The guarded applier additionally re-validates the actual changed files
    # after apply.
    try:
        assert_files_in_scope(code, chunk.files_expected)
    except ScopeDriftError as drift:
        report = build_patch_failure_report(
            PatchFailureType.SCOPE_VIOLATION,
            technical_details=str(drift),
            changed_files_attempted=[change.path for change in code.files_changed],
            allowed_files=chunk.files_expected,
            working_tree_clean=local_git.is_working_tree_clean(target_repo_path),
            chunk_number=chunk_number,
            failed_step="patch",
        )
        return _fail_chunk_with_report(run_id, chunk_number, report)

    outcome = apply_patch_guarded(
        code,
        run_id,
        chunk_number=chunk_number,
        files_expected=chunk.files_expected,
    )
    if not outcome.success:
        return _fail_chunk_with_report(run_id, chunk_number, outcome.failure)

    patch = outcome.patch_result
    test_result = run_tests(patch, run_id, chunk_number=chunk_number)

    # #28D: record the display-only runtime test verdict for BOTH pass and fail,
    # before any branch decision. This writes only the chunk's test_run_* columns
    # and cannot change the outcome below (pass/fail stays exit-code based).
    _persist_test_run_verdict(run_id, chunk_number, test_result)

    if not test_result.passed:
        # tester.py already attempted rollback on failure; verify cleanliness
        # and report. Do NOT roll back again here (avoids a double rollback).
        clean = local_git.is_working_tree_clean(target_repo_path)
        report = build_patch_failure_report(
            PatchFailureType.TEST_FAILURE_AFTER_APPLY,
            technical_details=getattr(test_result, "output", None),
            changed_files_attempted=[change.path for change in code.files_changed],
            allowed_files=chunk.files_expected,
            rollback_performed=True,
            working_tree_clean=clean,
            chunk_number=chunk_number,
            failed_step="test",
        )
        return _fail_chunk_with_report(run_id, chunk_number, report)

    if chunk.requires_human_review:
        return _pause_for_chunk_approval(run_id, chunk, code, branch_name)

    _commit_and_complete_chunk(run_id, chunk, code, target_repo_path, project_id, plan)
    return None


async def _execute_approved_chunks_locked(
    run_id: str,
    plan_status: ChunkPlanResponse,
) -> dict:
    """
    Execute pending chunks sequentially for an approved chunk plan.

    This is intentionally narrow: it does not push, create PRs, or perform
    per-chunk approval.
    """
    print(f"[CHUNKED] Starting execution | run_id={run_id}")

    if plan_status.chunk_plan_status != "approved":
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk plan is not approved. "
            f"run_id={run_id} | status={plan_status.chunk_plan_status}"
        )

    if _has_running_chunk(plan_status):
        return {
            "status": "already_running",
            "message": "Execution already in progress",
        }

    definitions = _definition_by_number(plan_status)
    project, project_runtime = _project_runtime_for_plan(plan_status)
    target_repo_path = project["repo_path"]

    try:
        _validate_target_repo(target_repo_path)
    except Exception as error:
        _update_run_status(run_id, "failed", "preflight_failed")
        raise RuntimeError(
            f"chunked_orchestrator.py: preflight failed. "
            f"run_id={run_id} | error={error}"
        )

    pending = _pending_chunks(plan_status)
    # DB memory-conflict gate (#16D-4): block a clear conflict on a DB-sensitive run
    # before any branch/patch/commit. A blocked run writes nothing. Non-blocking
    # cases (docs-only, ambiguous/unknown signal, no conflict, honored override) keep
    # the #16D-3 warning. Runs once per run.
    pause = _apply_db_memory_conflict_policy(
        run_id,
        plan_status.project_id,
        target_repo_path,
        _collect_files_expected(pending),
    )
    if pause is not None:
        return pause

    branch_name = f"pipewright/{run_id[:8]}"
    local_git.assert_not_on_stale_pipewright_branch(target_repo_path, run_id)
    local_git.create_or_checkout_branch(branch_name, target_repo_path)

    completed_chunks = 0
    # Dependency map kept fresh as chunks complete (#24A): seeded from every
    # chunk's current status (so deps already completed in a prior pass pass the
    # check), then updated locally after each completion to avoid both stale
    # blocking and a re-read per chunk.
    status_by_number = _status_by_number(plan_status)
    with active_project(project_runtime):
        for chunk_status in pending:
            chunk_number = chunk_status.chunk_number
            chunk = definitions[chunk_number]
            try:
                pause_result = await _execute_single_chunk(
                    run_id,
                    plan_status.project_id,
                    chunk,
                    target_repo_path,
                    branch_name,
                    status_by_number,
                )
                if pause_result is not None:
                    return pause_result
                status_by_number[chunk_number] = ChunkStatusValue.COMPLETED
                completed_chunks += 1
            except Exception as error:
                return _fail_chunk(run_id, chunk_number, error)

    result = _mark_awaiting_final_approval(
        run_id,
        plan_status,
        branch_name,
        target_repo_path,
    )
    result["completed_chunks"] = completed_chunks
    print(f"[CHUNKED] Awaiting final approval | run_id={run_id}")
    return result


async def execute_approved_chunks(run_id: str) -> dict:
    plan_status = get_chunk_plan_status(run_id)
    async with project_repo_lock(plan_status.project_id):
        return await _execute_approved_chunks_locked(run_id, plan_status)


async def _resume_chunked_pipeline_locked(
    run_id: str,
    plan_status: ChunkPlanResponse,
) -> dict:
    """
    Manually resume a failed or stale chunked run from chunk boundaries.

    This does not auto-clean worktrees, recreate branches, push, create PRs, or
    perform per-chunk approval.
    """
    print(f"[CHUNKED] Starting resume | run_id={run_id}")

    if plan_status.chunk_plan_status != "approved":
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk plan is not approved. "
            f"run_id={run_id} | status={plan_status.chunk_plan_status}"
        )

    definitions = _definition_by_number(plan_status)
    project, project_runtime = _project_runtime_for_plan(plan_status)
    target_repo_path = project["repo_path"]
    branch_name = f"pipewright/{run_id[:8]}"

    _validate_target_repo(target_repo_path, require_clean=False)
    _verify_resume_branch(branch_name, target_repo_path)

    awaiting_chunk = _awaiting_approval_chunk(plan_status)
    if awaiting_chunk is not None:
        if _get_pending_chunk_gate(run_id, awaiting_chunk.chunk_number):
            return {
                "status": "awaiting_chunk_approval",
                "run_id": run_id,
                "chunk_number": awaiting_chunk.chunk_number,
                "approval_required": True,
                "branch_name": branch_name,
            }

    _verify_clean_for_resume(target_repo_path)
    _reset_stale_running_chunks(run_id)
    _update_run_status(run_id, "running", "resume")

    refreshed = get_chunk_plan_status(run_id)
    skipped_chunks = 0
    completed_chunks = 0
    # Dependency map kept fresh as chunks complete/skip (#24A). Seeded from the
    # post-reset status of every chunk so an already-completed dependency passes.
    status_by_number = _status_by_number(refreshed)

    resumable = _resumable_chunks(refreshed)
    # DB memory-conflict gate (#16D-4): re-evaluate once before any chunk runs. An
    # approved override is honored only when the current conflict still matches; a
    # changed/new conflict re-blocks. Non-blocking cases keep the #16D-3 warning.
    pause = _apply_db_memory_conflict_policy(
        run_id,
        plan_status.project_id,
        target_repo_path,
        _collect_files_expected(resumable),
    )
    if pause is not None:
        return pause

    with active_project(project_runtime):
        for chunk_status in resumable:
            chunk_number = chunk_status.chunk_number
            chunk = definitions[chunk_number]
            checkpoint = load_chunk_step_checkpoint(run_id, chunk_number, "test")
            if checkpoint is not None:
                # Dependency guard before skip-completing (#24A): a valid test
                # checkpoint must not mark a chunk completed while a dependency is
                # still incomplete. Fail safe with DEPENDENCY_NOT_MET instead.
                unmet = _unmet_dependencies(chunk, status_by_number)
                if unmet:
                    return _fail_chunk(
                        run_id,
                        chunk_number,
                        _dependency_not_met_message(
                            chunk_number, unmet, status_by_number
                        ),
                    )
                try:
                    _verify_completed_checkpoint_safe(
                        run_id,
                        chunk,
                        chunk_status,
                        target_repo_path,
                    )
                    update_chunk_status(run_id, chunk_number, "completed")
                    _update_run_status(
                        run_id,
                        "running",
                        f"chunk_{chunk_number}_skipped",
                        chunk_number,
                    )
                    status_by_number[chunk_number] = ChunkStatusValue.COMPLETED
                    skipped_chunks += 1
                    completed_chunks += 1
                    continue
                except Exception as error:
                    _update_run_status(run_id, "failed", "resume_recovery_failed")
                    raise RuntimeError(str(error))

            try:
                pause_result = await _execute_single_chunk(
                    run_id,
                    plan_status.project_id,
                    chunk,
                    target_repo_path,
                    branch_name,
                    status_by_number,
                )
                if pause_result is not None:
                    return pause_result
                status_by_number[chunk_number] = ChunkStatusValue.COMPLETED
                completed_chunks += 1
            except Exception as error:
                return _fail_chunk(run_id, chunk_number, error)

    result = _mark_awaiting_final_approval(
        run_id,
        plan_status,
        branch_name,
        target_repo_path,
    )
    result["resumed"] = True
    result["completed_chunks"] = completed_chunks
    result["skipped_chunks"] = skipped_chunks
    print(f"[CHUNKED] Resume awaiting final approval | run_id={run_id}")
    return result


async def resume_chunked_pipeline(run_id: str) -> dict:
    plan_status = get_chunk_plan_status(run_id)
    async with project_repo_lock(plan_status.project_id):
        return await _resume_chunked_pipeline_locked(run_id, plan_status)


def _approve_chunk_and_commit_locked(
    run_id: str,
    chunk_number: int,
    plan_status: ChunkPlanResponse,
) -> dict:
    """
    Approve a pending high-risk chunk and commit its already-tested files.
    """
    print(
        f"[CHUNKED] Approving chunk | "
        f"run_id={run_id} | chunk={chunk_number}"
    )
    definitions = _definition_by_number(plan_status)
    if chunk_number not in definitions:
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk definition missing. "
            f"run_id={run_id} | chunk={chunk_number}"
        )

    chunk_status = next(
        (chunk for chunk in plan_status.chunks if chunk.chunk_number == chunk_number),
        None,
    )
    if chunk_status is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk not found. "
            f"run_id={run_id} | chunk={chunk_number}"
        )
    if chunk_status.status != "awaiting_chunk_approval":
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk is not awaiting approval. "
            f"run_id={run_id} | chunk={chunk_number} | status={chunk_status.status}"
        )

    project, _runtime = _project_runtime_for_plan(plan_status)
    target_repo_path = project["repo_path"]
    _validate_target_repo(target_repo_path, require_clean=False)

    code = _load_code_from_checkpoint(run_id, chunk_number)
    _decide_pending_chunk_gate(run_id, chunk_number, "approved")
    _commit_and_complete_chunk(
        run_id,
        definitions[chunk_number],
        code,
        target_repo_path,
        plan_status.project_id,
    )
    _update_run_status(run_id, "chunk_approved", "chunk_approved", chunk_number)
    return {
        "status": "chunk_approved",
        "run_id": run_id,
        "chunk_number": chunk_number,
        "next_action": f"call /runs/{run_id}/chunks/resume to continue",
    }


def approve_chunk_and_commit(run_id: str, chunk_number: int) -> dict:
    plan_status = get_chunk_plan_status(run_id)
    with project_repo_lock_sync(plan_status.project_id):
        return _approve_chunk_and_commit_locked(run_id, chunk_number, plan_status)


def _reject_chunk_and_rollback_locked(
    run_id: str,
    chunk_number: int,
    plan_status: ChunkPlanResponse,
    reason: str | None = None,
) -> dict:
    """
    Reject a pending high-risk chunk, rollback its patch, and fail the run.
    """
    print(
        f"[CHUNKED] Rejecting chunk | "
        f"run_id={run_id} | chunk={chunk_number}"
    )
    chunk_status = next(
        (chunk for chunk in plan_status.chunks if chunk.chunk_number == chunk_number),
        None,
    )
    if chunk_status is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk not found. "
            f"run_id={run_id} | chunk={chunk_number}"
        )
    if chunk_status.status != "awaiting_chunk_approval":
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk is not awaiting approval. "
            f"run_id={run_id} | chunk={chunk_number} | status={chunk_status.status}"
        )

    project, project_runtime = _project_runtime_for_plan(plan_status)
    target_repo_path = project["repo_path"]
    _validate_target_repo(target_repo_path, require_clean=False)

    with active_project(project_runtime):
        rollback_ok = rollback_patch(run_id, chunk_number)
    if not rollback_ok:
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk rollback failed or was unavailable. "
            f"run_id={run_id} | chunk={chunk_number}"
        )

    try:
        local_git.ensure_clean_worktree(target_repo_path)
    except Exception as error:
        dirty_files = []
        try:
            dirty_files = local_git.get_dirty_files(target_repo_path)
        except Exception:
            pass
        detail = ", ".join(dirty_files) if dirty_files else str(error)
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk rollback did not clean worktree. "
            f"run_id={run_id} | chunk={chunk_number} | dirty={detail}"
        )

    _decide_pending_chunk_gate(
        run_id,
        chunk_number,
        "rejected",
        reason or "Chunk approval rejected",
    )
    update_chunk_status(
        run_id,
        chunk_number,
        "rejected",
        reason or "Chunk approval rejected",
    )
    _update_run_status(run_id, "failed", f"chunk_{chunk_number}_rejected", chunk_number)
    return {
        "status": "chunk_rejected",
        "run_id": run_id,
        "chunk_number": chunk_number,
    }


def reject_chunk_and_rollback(
    run_id: str,
    chunk_number: int,
    reason: str | None = None,
) -> dict:
    plan_status = get_chunk_plan_status(run_id)
    with project_repo_lock_sync(plan_status.project_id):
        return _reject_chunk_and_rollback_locked(
            run_id,
            chunk_number,
            plan_status,
            reason,
        )


# --------------------------------------------------------------------------- #
# Human-triggered patch retry execution (#26D2)
#
# Internal only — no public route wires this yet (that is #26D3). A failed chunk
# whose stored PatchFailureReport is human-retryable (per the #26D1 eligibility
# helper) can be re-coded against the CURRENT working tree, re-validated against
# the UNCHANGED files_expected, applied, and tested. On success the chunk pauses
# at the EXISTING awaiting_chunk_approval gate with a recovered_patch_review
# marker and is committed only later through the existing approval path — never
# here. This introduces no new chunk status, checkpoint type, or commit site,
# never re-triages, never runs the planner, never mutates files_expected, and
# never weakens scope_guard.
# --------------------------------------------------------------------------- #


def _load_chunk_failure_report(
    plan_status: ChunkPlanResponse,
    chunk_number: int,
) -> PatchFailureReport | None:
    """
    Parse a chunk's stored completion_summary into a PatchFailureReport.

    Returns None for a missing/malformed/non-failure summary (e.g. a normal
    success summary or a recovered_patch_review marker) so the eligibility helper
    rejects it safely. Pure read; never raises.
    """
    chunk = next(
        (item for item in plan_status.chunks if item.chunk_number == chunk_number),
        None,
    )
    if chunk is None or not chunk.completion_summary:
        return None
    raw = chunk.completion_summary
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    return patch_failure_report_from_completion_summary(value)


def _surface_files_expected_for_edit(
    plan: PlannerHandoff,
    files_expected: list[str],
) -> PlannerHandoff:
    """
    Return a copy of ``plan`` with every approved file surfaced to the coder via
    files_to_modify (#26D2 audit correction #1).

    files_expected paths are removed from files_to_read (which refuses files over
    200 lines) and unioned into files_to_modify (which tolerates larger files for
    edit grounding). This is prompt context only — it grants NO write authority;
    the write scope stays files_expected, enforced by scope_guard and
    apply_patch_guarded. Paths that cannot be normalized are left untouched.
    """
    def _norm(path: str) -> str | None:
        try:
            return normalize_relative_path(path)
        except Exception:
            return None

    expected_norm = {n for n in (_norm(p) for p in files_expected) if n is not None}
    new_read = [
        path for path in plan.files_to_read if _norm(path) not in expected_norm
    ]
    new_modify = list(plan.files_to_modify)
    modify_norm = {n for n in (_norm(p) for p in new_modify) if n is not None}
    for path in files_expected:
        normalized = _norm(path)
        if normalized is not None and normalized not in modify_norm:
            new_modify.append(path)
            modify_norm.add(normalized)
    return plan.model_copy(
        update={"files_to_read": new_read, "files_to_modify": new_modify}
    )


def _retry_plan_for_chunk(run_id: str, chunk: ChunkDefinition) -> PlannerHandoff:
    """
    Build the plan a retry feeds to the coder WITHOUT re-running the planner.

    Reuses the chunk's existing plan checkpoint when present; otherwise builds a
    constrained fallback from the chunk definition alone (no new scope, no files
    beyond files_expected). In both cases the approved files_expected are made
    visible through files_to_modify so the coder can re-ground targeted edits
    against the current on-disk contents.
    """
    checkpoint = load_chunk_step_checkpoint(run_id, chunk.chunk_number, "plan")
    if checkpoint:
        try:
            plan = PlannerHandoff.model_validate(checkpoint["output"])
            return _surface_files_expected_for_edit(plan, chunk.files_expected)
        except Exception:
            pass

    fallback = PlannerHandoff(
        run_id=run_id,
        feature_description=chunk.description,
        goal=chunk.description,
        steps=[
            f"Regenerate the change for chunk {chunk.chunk_number} against the "
            "current file contents.",
        ],
        files_to_create=[],
        files_to_modify=[],
        files_to_read=[],
        out_of_scope=[],
        risks=[],
        suggested_memory_entries=[],
    )
    return _surface_files_expected_for_edit(fallback, chunk.files_expected)


def _retry_wrong_branch_result(
    run_id: str,
    chunk_number: int,
    expected_branch: str,
    current_branch: str | None,
) -> dict:
    """
    Side-effect-free 409 for a retry whose repo HEAD is not on the run branch
    (#26D3a). Verify-only: no checkout, no branch creation, no switch — the user
    must checkout the run branch and retry. Mirrors _retry_ineligible_result's
    shape, with an extra ``detail`` naming the current and expected branches.
    ``current_branch`` is None when the branch could not be determined (e.g.
    detached HEAD or a git error).
    """
    if current_branch is not None:
        detail = (
            f"Target repo is on branch '{current_branch}', not the run branch "
            f"'{expected_branch}'. Checkout '{expected_branch}' and retry."
        )
    else:
        detail = (
            f"Could not determine the target repo's current branch (expected "
            f"'{expected_branch}'). Checkout '{expected_branch}' and retry."
        )
    print(
        f"[CHUNKED] Retry ineligible (wrong branch) | run_id={run_id} | "
        f"chunk={chunk_number} | expected={expected_branch} | "
        f"current={current_branch}"
    )
    return {
        "status": "retry_ineligible",
        "run_id": run_id,
        "chunk_number": chunk_number,
        "eligible": False,
        "reason": RETRY_INELIGIBLE_WRONG_BRANCH,
        "status_code": 409,
        "detail": detail,
    }


def _retry_branch_precheck(
    run_id: str,
    chunk_number: int,
    repo_path: str,
) -> dict | None:
    """
    Read-only branch guard for a human retry (#26D3a).

    Verifies the target repo's HEAD is already on the run branch
    (``pipewright/{run_id[:8]}``) WITHOUT touching the repo: no checkout, no
    branch creation, no switch. Retry never moves the user's HEAD, so a rejected
    request is fully side-effect-free. Must be called inside the project repo lock
    and before any eligibility/execution work.

    Returns a retry_ineligible dict (409) when HEAD is on the wrong branch, the
    branch is missing, or the branch cannot be determined (detached HEAD / git
    error); returns None when HEAD is correctly on the run branch and retry may
    proceed.
    """
    expected_branch = f"pipewright/{run_id[:8]}"
    try:
        current_branch = local_git.get_current_branch(repo_path)
    except Exception as error:
        # Detached HEAD (get_current_branch raises on an empty branch) or a git
        # failure: cannot prove HEAD is on the run branch, so reject — never guess.
        print(
            f"[CHUNKED] Retry branch verification failed | run_id={run_id} | "
            f"chunk={chunk_number} | expected={expected_branch} | error={error}"
        )
        return _retry_wrong_branch_result(
            run_id, chunk_number, expected_branch, None
        )
    if current_branch != expected_branch:
        return _retry_wrong_branch_result(
            run_id, chunk_number, expected_branch, current_branch
        )
    return None


def _retry_ineligible_result(
    run_id: str,
    chunk_number: int,
    decision,
) -> dict:
    """Safe, side-effect-free result for a rejected retry (no work performed)."""
    print(
        f"[CHUNKED] Retry ineligible | run_id={run_id} | "
        f"chunk={chunk_number} | reason={decision.reason}"
    )
    return {
        "status": "retry_ineligible",
        "run_id": run_id,
        "chunk_number": chunk_number,
        "eligible": False,
        "reason": decision.reason,
        "status_code": decision.status_code,
    }


def _persist_retry_patch_failure(
    run_id: str,
    chunk_number: int,
    report: PatchFailureReport,
    prior_attempts: list,
    *,
    test_outcome: str = "not_run",
) -> dict:
    """
    Persist a fresh patch_failure summary for a failed retry attempt.

    Carries the prior attempt history forward and appends exactly ONE human
    retry attempt (#26D1 record_retry_attempt) with freshly minted ids, then
    marks the chunk failed and emits the slim stage_failed event. Mirrors
    _fail_chunk_with_report but for a human retry. Never commits; never mutates
    files_expected.
    """
    carried = report.model_copy(update={"attempts": list(prior_attempts)})
    enriched = record_retry_attempt(
        carried,
        failure_report_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        started_at=_utc_now(),
        recovery_mode="human",
        failure_type=report.failure_type,
        failed_step=report.failed_step,
        changed_files_attempted=list(report.changed_files_attempted),
        changed_files_actual=list(report.changed_files_actual),
        scope_ok=report.failure_type != PatchFailureType.SCOPE_VIOLATION,
        test_outcome=test_outcome,
        outcome=(
            "manual_intervention"
            if report.manual_intervention_needed
            else "failed"
        ),
        human_decision="retry",
        working_tree_clean=report.working_tree_clean,
        rollback_performed=report.rollback_performed,
    )
    save_chunk_completion_summary(
        run_id,
        chunk_number,
        patch_failure_report_to_completion_summary(enriched),
    )
    update_chunk_status(run_id, chunk_number, "failed", report.message)
    _update_run_status(
        run_id, "failed", f"chunk_{chunk_number}_failed", chunk_number
    )
    _publish_safe(Event(
        run_id=run_id,
        chunk_number=chunk_number,
        kind="stage_failed",
        stage="patch",
        level="error",
        message=report.message,
        data={
            "kind": "patch_failure",
            "failure_type": report.failure_type.value,
            "chunk_number": chunk_number,
            "failed_step": report.failed_step,
            "rollback_performed": report.rollback_performed,
            "working_tree_clean": report.working_tree_clean,
            "manual_intervention_needed": report.manual_intervention_needed,
            "stale_index_hint": report.stale_index_hint,
            "suggested_actions": report.suggested_actions,
            "changed_files_attempted_count": len(report.changed_files_attempted),
            "changed_files_actual_count": len(report.changed_files_actual),
        },
    ))
    # #27: a #26 retry that re-fails with an eligible clean SCOPE_VIOLATION also
    # surfaces a pending scope expansion request. Same additive, best-effort path
    # as initial execution; does not change this failed return value or #26's
    # public retry behavior.
    _surface_scope_expansion_if_eligible(run_id, chunk_number, enriched)
    return {
        "status": "failed",
        "run_id": run_id,
        "failed_chunk": chunk_number,
        "error": report.message,
        "failure_report_id": enriched.failure_report_id,
    }


def _pause_recovered_chunk(
    run_id: str,
    chunk: ChunkDefinition,
    code: CoderHandoff,
    branch_name: str,
    original_report: PatchFailureReport,
) -> dict:
    """
    Store the recovered_patch_review marker and pause at the existing chunk
    approval gate (#26D2 success path).

    Appends a human "recovered" attempt (outcome="recovered",
    test_outcome="passed") onto the prior attempt history and stores a
    RecoveredPatchReviewSummary in completion_summary, so the summary is NOT read
    back as a patch_failure. The regenerated patch is on disk but uncommitted; it
    is committed only later through the existing approval path, which loads the
    newest code checkpoint. No commit happens here.
    """
    recovery_attempt_id = str(uuid.uuid4())
    touched = _files_touched(code)
    enriched = record_retry_attempt(
        original_report,
        failure_report_id=str(uuid.uuid4()),
        attempt_id=recovery_attempt_id,
        started_at=_utc_now(),
        recovery_mode="human",
        failure_type=None,
        failed_step=None,
        changed_files_attempted=touched,
        changed_files_actual=touched,
        scope_ok=True,
        test_outcome="passed",
        outcome="recovered",
        human_decision="retry",
        # The regenerated patch is applied but not yet committed, so the working
        # tree is intentionally not clean at the pause point.
        working_tree_clean=False,
        rollback_performed=False,
    )
    summary = RecoveredPatchReviewSummary(
        failure_report_id=enriched.failure_report_id,
        recovery_attempt_id=recovery_attempt_id,
        attempts=enriched.attempts,
    )
    save_chunk_completion_summary(
        run_id,
        chunk.chunk_number,
        recovered_patch_review_to_completion_summary(summary),
    )
    return _pause_for_chunk_approval(run_id, chunk, code, branch_name)


async def _execute_retry_attempt(
    run_id: str,
    chunk_number: int,
    chunk: ChunkDefinition,
    plan_status: ChunkPlanResponse,
    project_runtime: ProjectRuntimeConfig,
    target_repo_path: str,
    prior_attempts: list,
    branch_name: str,
    report: PatchFailureReport,
) -> dict:
    """
    Regenerate, validate, apply, and test a retry inside the active project
    context (#26D2 audit correction #2: coder/apply/tester read the active
    project's repo path and test command).

    Returns the awaiting-approval result on success or a failed result for a
    modeled failure (scope/dry-run/apply/test). Raises only on a truly
    unexpected error; the caller turns that into a failed chunk.
    """
    with active_project(project_runtime):
        plan = _retry_plan_for_chunk(run_id, chunk)
        code = await run_coder(
            plan,
            run_id,
            chunk_number=chunk_number,
            project_id=plan_status.project_id,
        )

        # Pre-apply scope guard against the UNCHANGED files_expected.
        try:
            assert_files_in_scope(code, chunk.files_expected)
        except ScopeDriftError as drift:
            scope_report = build_patch_failure_report(
                PatchFailureType.SCOPE_VIOLATION,
                technical_details=str(drift),
                changed_files_attempted=[c.path for c in code.files_changed],
                allowed_files=chunk.files_expected,
                working_tree_clean=local_git.is_working_tree_clean(
                    target_repo_path
                ),
                chunk_number=chunk_number,
                failed_step="patch",
            )
            return _persist_retry_patch_failure(
                run_id, chunk_number, scope_report, prior_attempts
            )

        # Zero-mutation pre-apply validation (#26B). On failure nothing is
        # written, so apply/tests are skipped entirely.
        dry = dry_run_changes(code, target_repo_path)
        if not dry.ok:
            failure_type = classify_patch_failure(
                RuntimeError(dry.error_message or ""), phase="apply"
            )
            dry_report = build_patch_failure_report(
                failure_type,
                technical_details=dry.error_message,
                changed_files_attempted=[c.path for c in code.files_changed],
                allowed_files=chunk.files_expected,
                working_tree_clean=local_git.is_working_tree_clean(
                    target_repo_path
                ),
                chunk_number=chunk_number,
                failed_step="patch",
            )
            return _persist_retry_patch_failure(
                run_id, chunk_number, dry_report, prior_attempts
            )

        outcome = apply_patch_guarded(
            code,
            run_id,
            chunk_number=chunk_number,
            files_expected=chunk.files_expected,
        )
        if not outcome.success:
            return _persist_retry_patch_failure(
                run_id, chunk_number, outcome.failure, prior_attempts
            )

        # tester.py rolls back on failure; do NOT roll back again here.
        test_result = run_tests(
            outcome.patch_result, run_id, chunk_number=chunk_number
        )

        # #28D: record the display-only runtime test verdict on the retry path
        # too, for BOTH pass and fail, before any branch decision. Without this a
        # retried/recovered chunk would carry a NULL verdict, which the #28F final-
        # approval gate treats as "no acknowledgement required" — letting a weak
        # command (e.g. `python --version`) slip past unacknowledged. Display-only:
        # this writes only the chunk's test_run_* columns and never changes the
        # outcome below (pass/fail stays exit-code based via test_result.passed).
        _persist_test_run_verdict(run_id, chunk_number, test_result)

        if not test_result.passed:
            clean = local_git.is_working_tree_clean(target_repo_path)
            test_report = build_patch_failure_report(
                PatchFailureType.TEST_FAILURE_AFTER_APPLY,
                technical_details=getattr(test_result, "output", None),
                changed_files_attempted=[c.path for c in code.files_changed],
                allowed_files=chunk.files_expected,
                rollback_performed=True,
                working_tree_clean=clean,
                chunk_number=chunk_number,
                failed_step="test",
            )
            return _persist_retry_patch_failure(
                run_id,
                chunk_number,
                test_report,
                prior_attempts,
                test_outcome="failed",
            )

        # Success: pause at the existing approval gate. No commit here; the
        # existing approval path commits the newest code checkpoint later.
        return _pause_recovered_chunk(
            run_id, chunk, code, branch_name, report
        )


async def _retry_failed_chunk_locked(
    run_id: str,
    chunk_number: int,
    failure_report_id: str,
    plan_status: ChunkPlanResponse,
) -> dict:
    """
    Re-run a failed, human-retryable chunk against the current tree (#26D2).

    Assumes the project repo lock is already held by the caller. Validates
    eligibility (#26D1), reuses the existing plan (never re-plans), regenerates
    the coder handoff, re-validates scope + dry-run, applies, and tests. On any
    failure it persists a fresh patch_failure report with an appended human
    attempt and marks the chunk failed. On success it pauses at the existing
    awaiting_chunk_approval gate with a recovered_patch_review marker and does
    NOT commit.

    Never calls _execute_single_chunk, never runs the planner, never mutates
    files_expected, never weakens scope_guard, never commits.
    """
    if plan_status.chunk_plan_status != "approved":
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk plan is not approved. "
            f"run_id={run_id} | status={plan_status.chunk_plan_status}"
        )

    definitions = _definition_by_number(plan_status)
    chunk = definitions.get(chunk_number)
    chunk_status = next(
        (item for item in plan_status.chunks if item.chunk_number == chunk_number),
        None,
    )
    if chunk is None or chunk_status is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk not found for retry. "
            f"run_id={run_id} | chunk={chunk_number}"
        )

    project, project_runtime = _project_runtime_for_plan(plan_status)
    target_repo_path = project["repo_path"]
    _validate_target_repo(target_repo_path, require_clean=False)

    # Branch guard (#26D3a): retry runs against the working tree, so HEAD must
    # already be on this run's branch (pipewright/{run_id[:8]}). Verify-only — we
    # never checkout/create/switch (that would move the user's HEAD on a request
    # we may then reject). Read-only and placed before eligibility/execution so a
    # wrong/missing/undeterminable branch is rejected with a clean, side-effect-
    # free 409 (no chunk marked running, no coder/apply/test, no summary write).
    branch_block = _retry_branch_precheck(run_id, chunk_number, target_repo_path)
    if branch_block is not None:
        return branch_block

    # Eligibility (#26D1): a pure decision over already-computed observations. No
    # coder/apply/test/disk work happens unless this passes.
    report = _load_chunk_failure_report(plan_status, chunk_number)
    status_by_number = _status_by_number(plan_status)
    dependencies_met = not _unmet_dependencies(chunk, status_by_number)
    working_tree_clean = local_git.is_working_tree_clean(target_repo_path)
    decision = evaluate_patch_retry_eligibility(
        report,
        requested_failure_report_id=failure_report_id,
        dependencies_met=dependencies_met,
        working_tree_clean=working_tree_clean,
        chunk_status=chunk_status.status,
    )
    if not decision.eligible:
        return _retry_ineligible_result(run_id, chunk_number, decision)

    # report is non-None here (eligibility rejects a None/missing report).
    prior_attempts = list(report.attempts)
    branch_name = f"pipewright/{run_id[:8]}"

    # Enter retry execution. Mark running so a crash leaves a resumable chunk and
    # so the "must be failed" eligibility guard cannot pass twice concurrently.
    update_chunk_status(run_id, chunk_number, "running")
    _update_run_status(
        run_id, "running_chunks", f"chunk_{chunk_number}_retry", chunk_number
    )

    # Execute the regeneration. An unexpected raise (e.g. the coder LLM or the
    # test subprocess failing hard) must still leave the chunk failed, not stuck
    # running — mirroring the orchestrator's own execution guard
    # (_execute_approved_chunks_locked).
    try:
        return await _execute_retry_attempt(
            run_id,
            chunk_number,
            chunk,
            plan_status,
            project_runtime,
            target_repo_path,
            prior_attempts,
            branch_name,
            report,
        )
    except Exception as error:
        return _fail_chunk(run_id, chunk_number, error)


async def retry_failed_chunk(
    run_id: str,
    chunk_number: int,
    failure_report_id: str,
) -> dict:
    """
    Internal entry point for a human-triggered patch retry (#26D2).

    Acquires the async project repo lock and delegates to
    _retry_failed_chunk_locked. No public route calls this yet (that is #26D3).

    The only pre-lock read is the run's project_id, which is the immutable lock
    key (it never changes for a run, and the lock cannot be acquired without it).
    All eligibility-relevant state — chunk status, the stored failure report, its
    failure_report_id, and the human attempt history — is loaded fresh *inside*
    the lock so a concurrent double-submit cannot bypass MAX_HUMAN_RETRIES on a
    stale snapshot (TOCTOU fix, #26D2).
    """
    project_id = get_chunk_plan_status(run_id).project_id
    async with project_repo_lock(project_id):
        plan_status = get_chunk_plan_status(run_id)
        return await _retry_failed_chunk_locked(
            run_id, chunk_number, failure_report_id, plan_status
        )


# ---------------------------------------------------------------------------
# Scope expansion approve-and-retry (#27E)
# ---------------------------------------------------------------------------


def _scope_expansion_ineligible_error(decision) -> Exception:
    """
    Map an ineligible #27 eligibility decision onto the right typed error so the
    route maps it to the documented status code (§11/§12): 422 for validation /
    eligibility (not-scope-violation, cap-exhausted, no-requestable-files) and 409
    for state conflicts (dirty tree, manual intervention). Both keep the request
    pending and mutate nothing.
    """
    message = (
        f"chunked_orchestrator.py: scope expansion not eligible "
        f"(reason={decision.reason})"
    )
    if decision.status_code == 422:
        return ScopeExpansionValidationError(decision.reason or "ineligible", message)
    return ScopeExpansionConflictError(message)


async def _approve_and_retry_scope_expansion_locked(
    run_id: str,
    chunk_number: int,
    request_id: str,
    approved_files: list[str],
    plan_status: ChunkPlanResponse,
    *,
    reason: str | None = None,
    decided_by: str | None = None,
) -> dict:
    """
    Approve a pending scope expansion request and re-drive the chunk retry under
    the amended effective scope (#27E). Assumes the project repo lock is already
    held by the caller; all eligibility-relevant state is loaded fresh here.

    Mandatory order (design §11, must not be reordered):
      1/2. (lock + fresh load — done by the caller / at the top here)
      3. request belongs to this run_id + chunk_number
      4. request is pending OR approved-but-not-applied (crash-window re-drive)
      5. chunk is still ``failed`` and carries a current patch-failure report
      6. the request's failure_report_id is still the current one (not stale)
         + live dirty-tree re-check + #27 eligibility (pending path only)
      7. read-only branch precheck (verify-only; never checkout)
      8. validate approved_files (write-path safety + denylist + subset)
      9. any side-effect-free precheck failure -> typed error, request stays
         pending, nothing retried/committed, chunks.files_expected untouched
      10. atomically flip pending -> approved (persist approved_files)
      11. re-drive #26 internal execution (_execute_retry_attempt) with the
          effective scope already overlaid by get_chunk_plan_status
      12. flip approved -> applied once the retry has been driven

    Reuses #26's internal execution but NOT its public eligibility front door
    (which hard-rejects SCOPE_VIOLATION). Never calls _execute_single_chunk,
    never runs the planner, never mutates chunks.files_expected, never weakens
    scope_guard, and never commits. A successful expanded retry pauses at the
    existing awaiting_chunk_approval gate; commit happens later via the unchanged
    approval path.
    """
    if plan_status.chunk_plan_status != "approved":
        raise ScopeExpansionConflictError(
            f"chunked_orchestrator.py: chunk plan is not approved. "
            f"run_id={run_id} | status={plan_status.chunk_plan_status}"
        )

    # 3. Request must exist and belong to this run + chunk (else 404, no mutation).
    request = get_scope_expansion_request(request_id)  # ValueError -> 404
    if request.run_id != run_id or request.chunk_number != chunk_number:
        raise ValueError(
            "chunked_orchestrator.py: scope expansion request "
            f"{request_id} not found for run {run_id} chunk {chunk_number}"
        )

    # 4. Only a pending request may be newly approved; an approved-but-not-applied
    #    request is re-driven (crash-window idempotency, §14). applied / rejected /
    #    superseded cannot be acted on (409).
    status = request.status
    if not (
        status == ScopeExpansionStatus.PENDING.value
        or status == ScopeExpansionStatus.APPROVED.value
    ):
        raise ScopeExpansionConflictError(
            "chunked_orchestrator.py: scope expansion request "
            f"{request_id} is {status}; it cannot be approved or retried again"
        )

    # 5. Chunk must still be failed and carry a current patch-failure report.
    chunk_status = next(
        (item for item in plan_status.chunks if item.chunk_number == chunk_number),
        None,
    )
    if chunk_status is None:
        raise ValueError(
            f"chunked_orchestrator.py: chunk not found for scope retry. "
            f"run_id={run_id} | chunk={chunk_number}"
        )
    if chunk_status.status != ChunkStatusValue.FAILED:
        raise ScopeExpansionConflictError(
            f"chunked_orchestrator.py: chunk {chunk_number} is not failed "
            f"(status={chunk_status.status}); scope retry refused"
        )
    report = _load_chunk_failure_report(plan_status, chunk_number)
    if report is None:
        raise ScopeExpansionConflictError(
            f"chunked_orchestrator.py: no current patch failure for chunk "
            f"{chunk_number}; scope retry refused"
        )

    # 6. Optimistic-concurrency: the request must still be tied to the current
    #    failure. If the chunk has since re-failed (a new failure_report_id), the
    #    request is stale -> 409, nothing mutated.
    if (
        not report.failure_report_id
        or report.failure_report_id != request.failure_report_id
    ):
        raise ScopeExpansionConflictError(
            "chunked_orchestrator.py: scope expansion request is stale "
            "(the chunk's current failure no longer matches this request)"
        )

    project, project_runtime = _project_runtime_for_plan(plan_status)
    target_repo_path = project["repo_path"]
    _validate_target_repo(target_repo_path, require_clean=False)

    # Live dirty-tree re-check (§13/§18): even if the stored report says clean, a
    # tree that went dirty since the failure refuses scope approval. Dirty tree
    # means manual intervention only.
    working_tree_clean = local_git.is_working_tree_clean(target_repo_path)
    # Re-evaluate #27 eligibility inside the lock for a fresh approval. (A
    # re-drive's request is already approved/in-force, so the cap check would
    # double-count it; the gates that still matter for a re-drive — failed chunk,
    # matching report, clean tree, correct branch — are all checked above/below.)
    approve_decision = evaluate_scope_expansion_approve_retry_eligibility(
        chunk_plan_status=plan_status.chunk_plan_status,
        request_status=request.status,
        chunk_status=chunk_status.status,
        has_patch_failure_report=report is not None,
        report_failure_report_id=report.failure_report_id if report else None,
        request_failure_report_id=request.failure_report_id,
        working_tree_clean=working_tree_clean,
        failure_type=report.failure_type if report else None,
        manual_intervention_needed=(
            report.manual_intervention_needed if report else False
        ),
        amendments_used=count_in_force_scope_amendments(run_id, chunk_number),
        requested_extra_files=list(request.requested_files),
    )
    if not approve_decision.eligible:
        if approve_decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_PLAN_NOT_APPROVED:
            raise ScopeExpansionConflictError(
                f"chunked_orchestrator.py: chunk plan is not approved. "
                f"run_id={run_id} | status={plan_status.chunk_plan_status}"
            )
        if approve_decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE:
            raise ScopeExpansionConflictError(
                "chunked_orchestrator.py: scope expansion request "
                f"{request_id} is {request.status}; it cannot be approved or retried again"
            )
        if approve_decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_CHUNK_NOT_FAILED:
            raise ScopeExpansionConflictError(
                f"chunked_orchestrator.py: chunk {chunk_number} is not failed "
                f"(status={chunk_status.status}); scope retry refused"
            )
        if approve_decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_MISSING_REPORT:
            raise ScopeExpansionConflictError(
                f"chunked_orchestrator.py: no current patch failure for chunk "
                f"{chunk_number}; scope retry refused"
            )
        if approve_decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_STALE_REPORT:
            raise ScopeExpansionConflictError(
                "chunked_orchestrator.py: scope expansion request is stale "
                "(the chunk's current failure no longer matches this request)"
            )
        if approve_decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_DIRTY_WORKTREE:
            raise ScopeExpansionConflictError(
                "chunked_orchestrator.py: working tree is not clean; scope approval "
                "refused (manual intervention required)"
            )
        if approve_decision.scope_decision is not None:
            raise _scope_expansion_ineligible_error(approve_decision.scope_decision)
        raise ScopeExpansionConflictError(
            "chunked_orchestrator.py: scope expansion request cannot be approved "
            f"or retried (reason={approve_decision.reason})"
        )

    is_pending = approve_decision.is_pending

    # 7. Read-only branch precheck. A wrong/missing/undeterminable branch returns
    #    a side-effect-free 409 dict and leaves the request pending — retry runs
    #    against the working tree, so it must never move HEAD on a request it may
    #    reject.
    branch_block = _retry_branch_precheck(run_id, chunk_number, target_repo_path)
    if branch_block is not None:
        return branch_block

    # 8/10. Validate the human-approved allowlist and flip pending -> approved.
    #       For a re-drive the approval already happened: keep the persisted
    #       approved_files (approval is immutable once granted) and resume the
    #       retry.
    if is_pending:
        validated = validate_approved_files(request.requested_files, approved_files)
        original_norm = {
            normalized
            for normalized in (
                _safe_norm(path) for path in chunk_status.files_expected
            )
            if normalized is not None
        }
        if all(path in original_norm for path in validated):
            raise ScopeExpansionValidationError(
                "approved_files_no_new_scope",
                "scope_expansion.py: approved files add nothing beyond the "
                "original scope; an empty amendment is not approved.",
            )
        update_scope_expansion_request_status(
            request_id,
            ScopeExpansionStatus.APPROVED,
            approved_files=validated,
            decision_reason=reason,
            decided_by=decided_by,
        )

    # Reload the plan so the effective-scope overlay now includes the approved
    # files (single merge site: get_chunk_plan_status). _definition_by_number
    # carries that effective files_expected into the ChunkDefinition the retry —
    # and therefore scope_guard — sees.
    fresh_plan = get_chunk_plan_status(run_id)
    definitions = _definition_by_number(fresh_plan)
    chunk = definitions.get(chunk_number)
    if chunk is None:
        raise RuntimeError(
            f"chunked_orchestrator.py: chunk definition missing for scope retry. "
            f"run_id={run_id} | chunk={chunk_number}"
        )

    prior_attempts = list(report.attempts)
    branch_name = f"pipewright/{run_id[:8]}"

    # Enter retry execution. Mark running so a crash leaves a resumable chunk.
    update_chunk_status(run_id, chunk_number, "running")
    _update_run_status(
        run_id, "running_chunks", f"chunk_{chunk_number}_scope_retry", chunk_number
    )

    # 11/12. Drive the retry under the amended effective scope, then flip
    #        approved -> applied: the approval has been consumed by an attempt
    #        that wrote a fresh result. Only a hard process crash before this
    #        leaves the request 'approved' (re-drivable, §14).
    try:
        try:
            result = await _execute_retry_attempt(
                run_id,
                chunk_number,
                chunk,
                fresh_plan,
                project_runtime,
                target_repo_path,
                prior_attempts,
                branch_name,
                report,
            )
        except Exception as error:
            result = _fail_chunk(run_id, chunk_number, error)
    finally:
        try:
            update_scope_expansion_request_status(
                request_id, ScopeExpansionStatus.APPLIED
            )
        except ValueError:
            # Already terminal (e.g. a concurrent transition) — leave as-is.
            pass
    return result


def _safe_norm(path: str) -> str | None:
    try:
        return normalize_relative_path(path)
    except Exception:
        return None


async def approve_and_retry_scope_expansion(
    run_id: str,
    chunk_number: int,
    request_id: str,
    approved_files: list[str],
    *,
    reason: str | None = None,
    decided_by: str | None = None,
) -> dict:
    """
    Public entry point for scope-expansion approve-and-retry (#27E).

    Acquires the async project repo lock (the same lock #26 uses) and delegates
    to _approve_and_retry_scope_expansion_locked, which loads all
    eligibility-relevant state fresh inside the lock. The only pre-lock read is
    the run's immutable project_id (the lock key). Scope approval is NOT code
    approval: a successful expanded retry still pauses at awaiting_chunk_approval
    for human review and is committed only later by the unchanged approval path.
    """
    project_id = get_chunk_plan_status(run_id).project_id
    async with project_repo_lock(project_id):
        plan_status = get_chunk_plan_status(run_id)
        return await _approve_and_retry_scope_expansion_locked(
            run_id,
            chunk_number,
            request_id,
            list(approved_files),
            plan_status,
            reason=reason,
            decided_by=decided_by,
        )
