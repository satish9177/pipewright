"""
startup_recovery.py
Repairs interrupted in-process run state after server restart.

DB-state reconciliation is the only thing this module changes. Git working-tree
recovery is intentionally NOT automated: #32E adds read-only detection of a
possibly-dirty target repo for an interrupted run and surfaces human-gated
guidance (log + structured result) only. It never resets, stashes, checks out,
cleans, commits, or resumes anything.
"""

import logging

from sqlalchemy import text

from backend.core.statuses import ChunkStatusValue, RunStatus
from backend.db.database import engine
from backend.git.local_git import detect_uncommitted_changes


logger = logging.getLogger(__name__)


RESTART_RECOVERY_MESSAGE = "Reset after server restart; resume required."

# Human-gated guidance shown when an interrupted run's target repo is dirty.
# Pipewright takes NO Git action; the operator decides.
INTERRUPTED_DIRTY_TREE_MESSAGE = (
    "This run was interrupted and the target repo has uncommitted changes. "
    "Review with `git status`. Pipewright will not auto-reset, auto-stash, "
    "auto-resume, or auto-commit. Decide manually whether to keep or discard "
    "the changes."
)


def _detect_interrupted_dirty_repos() -> list[dict]:
    """
    Read-only, best-effort interruption guidance for interrupted runs.

    For every run currently marked interrupted, inspect its project repo path
    (if any) with strictly read-only Git detection and surface guidance for a
    dirty or un-inspectable tree. NEVER mutates a repo and NEVER raises — a
    failure here must not block startup recovery. Clean repos produce no entry.
    """
    entries: list[dict] = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT r.id AS run_id, p.repo_path AS repo_path
                FROM pipeline_runs r
                LEFT JOIN projects p ON p.id = r.project_id
                WHERE r.status = :interrupted
            """), {"interrupted": RunStatus.INTERRUPTED}).fetchall()
    except Exception as error:
        logger.warning(
            "startup_recovery.py: could not query interrupted runs for "
            "dirty-tree guidance: %s",
            error,
        )
        return entries

    for row in rows:
        run_id = row._mapping["run_id"]
        repo_path = row._mapping["repo_path"]
        # No project / repo path on the run: nothing actionable to inspect.
        if not repo_path or not str(repo_path).strip():
            continue

        status = detect_uncommitted_changes(repo_path)
        if status.state == "dirty":
            logger.warning(
                "[RECOVERY] Interrupted run %s: target repo has uncommitted "
                "changes (%d file(s)). %s",
                run_id, len(status.dirty_files), INTERRUPTED_DIRTY_TREE_MESSAGE,
            )
            entries.append({
                "run_id": run_id,
                "repo_path": repo_path,
                "state": "dirty",
                "dirty_files": list(status.dirty_files),
                "guidance": INTERRUPTED_DIRTY_TREE_MESSAGE,
            })
        elif status.state in ("missing_path", "not_a_repo", "error"):
            logger.warning(
                "[RECOVERY] Interrupted run %s: could not inspect target repo "
                "(%s: %s). No Git action was taken.",
                run_id, status.state, status.reason,
            )
            entries.append({
                "run_id": run_id,
                "repo_path": repo_path,
                "state": status.state,
                "dirty_files": [],
                "guidance": status.reason,
            })
        # state == "clean" -> no guidance needed.

    return entries


def recover_interrupted_runs() -> dict:
    try:
        with engine.begin() as conn:
            chunk_result = conn.execute(text("""
                UPDATE chunks
                SET status = :pending,
                    started_at = NULL,
                    error_message = COALESCE(NULLIF(error_message, ''), :message)
                WHERE status = :running
            """), {
                "pending": ChunkStatusValue.PENDING,
                "running": ChunkStatusValue.RUNNING,
                "message": RESTART_RECOVERY_MESSAGE,
            })
            run_result = conn.execute(text("""
                UPDATE pipeline_runs
                SET status = :interrupted,
                    current_step = :interrupted
                WHERE status IN (:running, :running_chunks)
            """), {
                "interrupted": RunStatus.INTERRUPTED,
                "running": RunStatus.RUNNING,
                "running_chunks": RunStatus.RUNNING_CHUNKS,
            })
        # Read-only interruption guidance AFTER DB reconciliation has committed.
        # Detection only: never mutates the repo and never raises.
        dirty_tree_guidance = _detect_interrupted_dirty_repos()
        return {
            "chunks_reset": chunk_result.rowcount,
            "runs_interrupted": run_result.rowcount,
            "dirty_tree_guidance": dirty_tree_guidance,
        }
    except Exception as error:
        raise RuntimeError(f"startup_recovery.py: recovery failed: {error}")
