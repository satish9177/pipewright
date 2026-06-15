"""
run_turn_store.py

Append-only run turn (conversation) log for Phase 3 item 13 and §23 row 7b.

One row per human steer/plan-turn message: the sanitized text, the target type,
the targeted chunk or plan sentinel, and a link to the chunk_attempts row when
the turn produced an executed chunk attempt. The original
pipeline_runs.feature_description stays immutable; turns are additive context.
Rows store user text and metadata only: never diffs, test output, provider/Git
errors, prompts, or secrets. The table is append-only by convention: this module
inserts and reads; nothing ever updates or deletes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.db.database import engine
from backend.llm.sanitize import sanitize_for_log

RUN_TURN_TARGET_CHUNK = "chunk"
RUN_TURN_TARGET_PLAN = "plan"
PLAN_TURN_CHUNK_SENTINEL = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_turn(
    *,
    run_id: str,
    project_id: str,
    target_type: str,
    chunk_number: int,
    steer_text: str,
    attempt_id: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    if target_type not in {RUN_TURN_TARGET_CHUNK, RUN_TURN_TARGET_PLAN}:
        raise ValueError(f"run_turn_store.py: unsupported target_type={target_type}")

    turn_id = str(uuid.uuid4())
    sanitized_text = sanitize_for_log(steer_text)
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT COALESCE(MAX(turn_number), 0) + 1
                FROM run_turns
                WHERE run_id = :run_id
            """),
            {"run_id": run_id},
        ).fetchone()
        turn_number = int(row[0] if row is not None else 1)
        created_at = _utc_now()
        conn.execute(
            text("""
                INSERT INTO run_turns (
                    id,
                    run_id,
                    project_id,
                    target_type,
                    chunk_number,
                    turn_number,
                    steer_text,
                    attempt_id,
                    outcome,
                    created_at
                )
                VALUES (
                    :id,
                    :run_id,
                    :project_id,
                    :target_type,
                    :chunk_number,
                    :turn_number,
                    :steer_text,
                    :attempt_id,
                    :outcome,
                    :created_at
                )
            """),
            {
                "id": turn_id,
                "run_id": run_id,
                "project_id": project_id,
                "target_type": target_type,
                "chunk_number": chunk_number,
                "turn_number": turn_number,
                "steer_text": sanitized_text,
                "attempt_id": attempt_id,
                "outcome": outcome,
                "created_at": created_at,
            },
        )
    return {
        "id": turn_id,
        "run_id": run_id,
        "project_id": project_id,
        "target_type": target_type,
        "chunk_number": chunk_number,
        "turn_number": turn_number,
        "steer_text": sanitized_text,
        "attempt_id": attempt_id,
        "outcome": outcome,
        "created_at": created_at,
    }


def record_run_turn(
    *,
    run_id: str,
    project_id: str,
    chunk_number: int,
    steer_text: str,
    attempt_id: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    """
    Append one chunk-targeted run_turns row and return the inserted audit metadata.

    turn_number is monotonic per run. The steer text is redacted with the same
    sanitizer the failure reports use, so a secret pasted into a steer is never
    persisted verbatim. attempt_id links the turn to the chunk_attempts row the
    steered attempt produced; it is None only when no attempt could be linked
    (e.g. the best-effort ledger write was skipped).
    """
    return _record_turn(
        run_id=run_id,
        project_id=project_id,
        target_type=RUN_TURN_TARGET_CHUNK,
        chunk_number=chunk_number,
        steer_text=steer_text,
        attempt_id=attempt_id,
        outcome=outcome,
    )


def record_plan_turn(
    *,
    run_id: str,
    project_id: str,
    message: str,
) -> dict[str, Any]:
    """
    Append one plan-targeted turn for a still-unapproved chunk plan.

    Plan turns use chunk_number=0 by design instead of making the historical
    chunk_number column nullable. They are advisory input only; this row never
    grants scope, approves a plan, executes code, or binds approval to a version.
    """
    return _record_turn(
        run_id=run_id,
        project_id=project_id,
        target_type=RUN_TURN_TARGET_PLAN,
        chunk_number=PLAN_TURN_CHUNK_SENTINEL,
        steer_text=message,
    )


def list_run_turns(
    run_id: str,
    chunk_number: int | None = None,
    target_type: str | None = None,
) -> list[dict[str, Any]]:
    where = "WHERE run_id = :run_id"
    params: dict[str, Any] = {"run_id": run_id}
    if chunk_number is not None:
        where += " AND chunk_number = :chunk_number"
        params["chunk_number"] = chunk_number
    if target_type is not None:
        where += " AND target_type = :target_type"
        params["target_type"] = target_type
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT *
                FROM run_turns
                {where}
                ORDER BY turn_number ASC, created_at ASC
            """),
            params,
        ).mappings().all()
    return [dict(row) for row in rows]
