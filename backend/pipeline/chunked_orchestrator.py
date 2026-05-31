"""
chunked_orchestrator.py
Phase 2B-4 chunk execution for approved chunk plans.

This module executes approved chunks one at a time. It does not implement
remote push, GitHub PR creation, or remote branch management.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from backend.core.status_service import (
    publish_chunk_status_changed as _publish_chunk_status_changed,
    publish_run_status_changed as _publish_run_status_changed,
    update_chunk_status as _service_update_chunk_status,
    update_run_status as _service_update_run_status,
)
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
)
from backend.memory.conflict_scope import is_db_sensitive_run
from backend.memory.repo_reality import evaluate_db_memory_conflicts
from backend.pipeline.coder import run_coder
from backend.pipeline.patch_applier import apply_patch, rollback_patch
from backend.pipeline.planner import run_planner
from backend.pipeline.run_locks import project_repo_lock, project_repo_lock_sync
from backend.pipeline.scope_guard import ScopeDriftError, assert_files_in_scope
from backend.pipeline.tester import run_tests
from backend.projects.project_context import ProjectRuntimeConfig, active_project
from backend.projects.project_store import require_project
from backend.repo.repo_indexer import get_relevant_files

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

    Evaluates DB memory vs. repo reality READ-ONLY (never mutates memory). On a clear
    conflict for a DB-sensitive run with no matching approved override, it creates a
    blocking ``memory_conflict`` gate, pauses the run, and returns a pause result.
    Otherwise it preserves the #16D-3 non-blocking warning and returns ``None``.
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


def _commit_and_complete_chunk(
    run_id: str,
    chunk: ChunkDefinition | ChunkStatus,
    coder_output: CoderHandoff,
    target_repo_path: str,
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


async def _execute_single_chunk(
    run_id: str,
    project_id: str,
    chunk: ChunkDefinition,
    target_repo_path: str,
    branch_name: str,
) -> dict | None:
    chunk_number = chunk.chunk_number
    update_chunk_status(run_id, chunk_number, "running")
    _update_run_status(
        run_id,
        "running_chunks",
        f"chunk_{chunk_number}",
        chunk_number,
    )

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
        return _fail_chunk(run_id, chunk_number, NO_CHANGES_MESSAGE)

    try:
        assert_files_in_scope(code, chunk.files_expected)
    except ScopeDriftError as drift:
        return _fail_chunk(run_id, chunk_number, str(drift))

    patch = apply_patch(code, run_id, chunk_number=chunk_number)
    test_result = run_tests(patch, run_id, chunk_number=chunk_number)

    if not test_result.passed:
        raise RuntimeError("Tests failed. Rollback triggered.")

    if chunk.requires_human_review:
        return _pause_for_chunk_approval(run_id, chunk, code, branch_name)

    _commit_and_complete_chunk(run_id, chunk, code, target_repo_path, plan)
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
                )
                if pause_result is not None:
                    return pause_result
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
                )
                if pause_result is not None:
                    return pause_result
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
