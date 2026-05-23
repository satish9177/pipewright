"""
chunked_orchestrator.py
Phase 2B-4A sequential execution for approved chunk plans.

This module executes approved chunks one at a time. It does not implement
resume, final approval, remote push, or PR creation.
"""

from pathlib import Path

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.git import local_git
from backend.models.chunk import ChunkDefinition, ChunkPlanResponse, ChunkStatus
from backend.models.handoff import CoderHandoff, PlannerHandoff
from backend.pipeline.chunk_store import (
    get_chunk_plan_status,
    get_previous_chunks_context,
    save_chunk_completion_summary,
    update_chunk_status,
)
from backend.pipeline.coder import run_coder
from backend.pipeline.patch_applier import apply_patch
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


def _has_running_chunk(plan: ChunkPlanResponse) -> bool:
    return any(chunk.status == "running" for chunk in plan.chunks)


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


def _validate_target_repo(repo_path: str) -> None:
    target_repo = Path(repo_path)
    if not target_repo.exists() or not target_repo.is_dir():
        raise RuntimeError(
            f"chunked_orchestrator.py: target repo missing: {repo_path}"
        )
    local_git.ensure_git_repo(repo_path)
    local_git.ensure_clean_worktree(repo_path)


async def execute_approved_chunks(run_id: str) -> dict:
    """
    Execute pending chunks sequentially for an approved chunk plan.

    This is intentionally narrow: it does not resume, push, create PRs, or
    perform final approval.
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
    project = require_project(plan_status.project_id)
    target_repo_path = project["repo_path"]
    project_runtime = ProjectRuntimeConfig(
        project_id=project["id"],
        repo_path=target_repo_path,
        test_command=project["test_command"],
    )

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
                update_chunk_status(run_id, chunk_number, "running")
                _update_run_status(
                    run_id,
                    "running_chunks",
                    f"chunk_{chunk_number}",
                    chunk_number,
                )

                enriched_description = _build_enriched_feature_description(
                    run_id,
                    plan_status.project_id,
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
                    return _fail_chunk(
                        run_id,
                        chunk_number,
                        "Tests failed. Rollback triggered.",
                    )

                touched_files = _files_touched(code)
                commit_message = f"chunk {chunk_number}: {chunk.title}"
                local_git.commit_files(
                    touched_files,
                    commit_message,
                    target_repo_path,
                )

                completion_summary = _build_completion_summary(chunk, plan, code)
                save_chunk_completion_summary(
                    run_id,
                    chunk_number,
                    completion_summary,
                )
                update_chunk_status(run_id, chunk_number, "completed")
                completed_chunks += 1
                print(
                    f"[CHUNKED] Chunk complete | "
                    f"run_id={run_id} | chunk={chunk_number}"
                )
            except Exception as error:
                return _fail_chunk(run_id, chunk_number, error)

    _update_run_status(
        run_id,
        "chunks_completed",
        "chunks_completed",
        plan_status.total_chunks,
    )
    print(f"[CHUNKED] Complete | run_id={run_id}")
    return {
        "status": "chunks_completed",
        "run_id": run_id,
        "completed_chunks": completed_chunks,
        "branch_name": branch_name,
    }
