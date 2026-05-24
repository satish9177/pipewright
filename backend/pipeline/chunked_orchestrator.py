"""
chunked_orchestrator.py
Phase 2B-4 chunk execution for approved chunk plans.

This module executes approved chunks one at a time. It does not implement
remote push, GitHub PR creation, or remote branch management.
"""

from pathlib import Path

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.git import local_git
from backend.checkpoint.checkpoint_store import load_chunk_step_checkpoint
from backend.models.chunk import ChunkDefinition, ChunkPlanResponse, ChunkStatus
from backend.models.handoff import CoderHandoff, PlannerHandoff
from backend.pipeline.approval_gate import (
    create_chunk_approval_gate_and_mark_chunk,
    create_final_approval_gate_and_mark_run,
)
from backend.pipeline.chunk_store import (
    get_chunk_plan_status,
    get_previous_chunks_context,
    save_chunk_completion_summary,
    update_chunk_status,
)
from backend.pipeline.coder import run_coder
from backend.pipeline.patch_applier import apply_patch, rollback_patch
from backend.pipeline.planner import run_planner
from backend.pipeline.tester import run_tests
from backend.projects.project_context import ProjectRuntimeConfig, active_project
from backend.projects.project_store import require_project
from backend.repo.repo_indexer import get_relevant_files


def _update_run_status(
    run_id: str,
    status: str,
    current_step: str,
    current_chunk_number: int | None = None,
) -> None:
    try:
        init_db()
        with engine.begin() as conn:
            if current_chunk_number is None:
                conn.execute(text("""
                    UPDATE pipeline_runs
                    SET status = :status,
                        current_step = :current_step
                    WHERE id = :run_id
                """), {
                    "run_id": run_id,
                    "status": status,
                    "current_step": current_step,
                })
            else:
                conn.execute(text("""
                    UPDATE pipeline_runs
                    SET status = :status,
                        current_step = :current_step,
                        current_chunk_number = :current_chunk_number
                    WHERE id = :run_id
                """), {
                    "run_id": run_id,
                    "status": status,
                    "current_step": current_step,
                    "current_chunk_number": current_chunk_number,
                })
    except Exception as error:
        raise RuntimeError(
            f"chunked_orchestrator.py: failed to update run status. "
            f"run_id={run_id} | error={error}"
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
    return {chunk.chunk_number: chunk for chunk in plan.triage.chunks}


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
        elif change.action == "modify":
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
                    decided_at = CURRENT_TIMESTAMP
                WHERE id = :gate_id
                  AND status = 'pending'
            """), {
                "status": status,
                "reason": reason,
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
    )
    code = await run_coder(plan, run_id, chunk_number=chunk_number)
    patch = apply_patch(code, run_id, chunk_number=chunk_number)
    test_result = run_tests(patch, run_id, chunk_number=chunk_number)

    if not test_result.passed:
        raise RuntimeError("Tests failed. Rollback triggered.")

    if chunk.requires_human_review:
        return _pause_for_chunk_approval(run_id, chunk, code, branch_name)

    _commit_and_complete_chunk(run_id, chunk, code, target_repo_path, plan)
    return None


async def execute_approved_chunks(run_id: str) -> dict:
    """
    Execute pending chunks sequentially for an approved chunk plan.

    This is intentionally narrow: it does not push, create PRs, or perform
    per-chunk approval.
    """
    print(f"[CHUNKED] Starting execution | run_id={run_id}")

    plan_status = get_chunk_plan_status(run_id)
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

    branch_name = f"pipewright/{run_id[:8]}"
    local_git.create_or_checkout_branch(branch_name, target_repo_path)

    completed_chunks = 0
    with active_project(project_runtime):
        for chunk_status in _pending_chunks(plan_status):
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


async def resume_chunked_pipeline(run_id: str) -> dict:
    """
    Manually resume a failed or stale chunked run from chunk boundaries.

    This does not auto-clean worktrees, recreate branches, push, create PRs, or
    perform per-chunk approval.
    """
    print(f"[CHUNKED] Starting resume | run_id={run_id}")

    plan_status = get_chunk_plan_status(run_id)
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

    with active_project(project_runtime):
        for chunk_status in _resumable_chunks(refreshed):
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


def approve_chunk_and_commit(run_id: str, chunk_number: int) -> dict:
    """
    Approve a pending high-risk chunk and commit its already-tested files.
    """
    print(
        f"[CHUNKED] Approving chunk | "
        f"run_id={run_id} | chunk={chunk_number}"
    )
    plan_status = get_chunk_plan_status(run_id)
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


def reject_chunk_and_rollback(
    run_id: str,
    chunk_number: int,
    reason: str | None = None,
) -> dict:
    """
    Reject a pending high-risk chunk, rollback its patch, and fail the run.
    """
    print(
        f"[CHUNKED] Rejecting chunk | "
        f"run_id={run_id} | chunk={chunk_number}"
    )
    plan_status = get_chunk_plan_status(run_id)
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

    _decide_pending_chunk_gate(
        run_id,
        chunk_number,
        "rejected",
        reason or "Chunk approval rejected",
    )
    rollback_patch(run_id, chunk_number)
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
