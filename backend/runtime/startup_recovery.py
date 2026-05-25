"""
startup_recovery.py
Repairs interrupted in-process run state after server restart.
"""

from sqlalchemy import text

from backend.core.statuses import ChunkStatusValue, RunStatus
from backend.db.database import engine


RESTART_RECOVERY_MESSAGE = "Reset after server restart; resume required."


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
        return {
            "chunks_reset": chunk_result.rowcount,
            "runs_interrupted": run_result.rowcount,
        }
    except Exception as error:
        raise RuntimeError(f"startup_recovery.py: recovery failed: {error}")
