"""
status_service.py
Small helpers for updating run/chunk statuses and publishing live-log events.

Event publishing is intentionally best-effort. A database status update must not
fail because a live-log subscriber or event bus path failed.
"""

import logging

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.events import event_bus
from backend.events.schema import Event

logger = logging.getLogger(__name__)


def _publish_event_safe(event: Event) -> None:
    try:
        event_bus.publish(event)
    except Exception as error:
        logger.warning("[EVENT_BUS] publish raised, ignored: %s", error)


def publish_run_status_changed(
    run_id: str,
    status: str,
    current_chunk_number: int | None = None,
) -> None:
    data = {"to_status": status}
    if current_chunk_number is not None:
        data["current_chunk_number"] = current_chunk_number

    _publish_event_safe(Event(
        run_id=run_id,
        kind="run_status_changed",
        stage="orchestrator",
        level="info",
        message=f"Run -> {status}",
        data=data,
    ))


def publish_chunk_status_changed(
    run_id: str,
    chunk_number: int,
    status: str,
) -> None:
    _publish_event_safe(Event(
        run_id=run_id,
        chunk_number=chunk_number,
        kind="chunk_status_changed",
        stage="orchestrator",
        level="info",
        message=f"Chunk {chunk_number} -> {status}",
        data={
            "chunk_number": chunk_number,
            "to_status": status,
        },
    ))


def update_run_status(
    run_id: str,
    status: str,
    current_step: str,
    current_chunk_number: int | None = None,
    publish_event: bool = False,
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
            f"status_service.py: failed to update run status. "
            f"run_id={run_id} | error={error}"
        )

    if publish_event:
        publish_run_status_changed(run_id, status, current_chunk_number)


def update_chunk_status(
    run_id: str,
    chunk_number: int,
    status: str,
    error_message: str | None = None,
    publish_event: bool = False,
) -> None:
    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE chunks
                SET status = :status,
                    error_message = :error_message
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "status": status,
                "error_message": error_message,
                "run_id": run_id,
                "chunk_number": chunk_number,
            })
    except Exception as error:
        raise RuntimeError(
            f"status_service.py: failed to update chunk status. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )

    if publish_event:
        publish_chunk_status_changed(run_id, chunk_number, status)
