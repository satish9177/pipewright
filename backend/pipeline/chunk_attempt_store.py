"""
chunk_attempt_store.py

Append-only chunk attempt ledger for Phase 2 item 12.

Rows are audit metadata only: no prompts, diffs, file contents, test output, or
secrets. The driver records one row for each attempt it drives over a chunk, and
resume uses completed rows as a fail-closed branch-HEAD drift check when they
exist. Older runs without rows degrade to the pre-ledger resume behavior.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from backend.db.database import engine


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def record_chunk_attempt(
    *,
    run_id: str,
    project_id: str,
    chunk_number: int,
    entry_mode: str,
    stage_outcomes: list[dict[str, Any]] | None = None,
    evidence_refs: list[str] | None = None,
    final_outcome_class: str | None = None,
    final_status: str | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """
    Append one chunk_attempts row and return the inserted audit metadata.

    attempt_number is monotonic per run/chunk. The table is append-only by
    convention: this helper inserts only; callers never update rows.
    """
    attempt_id = str(uuid.uuid4())
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM chunk_attempts
                WHERE run_id = :run_id
                  AND chunk_number = :chunk_number
            """),
            {"run_id": run_id, "chunk_number": chunk_number},
        ).fetchone()
        attempt_number = int(row[0] if row is not None else 1)
        created_at = _utc_now()
        conn.execute(
            text("""
                INSERT INTO chunk_attempts (
                    id,
                    run_id,
                    project_id,
                    chunk_number,
                    attempt_number,
                    entry_mode,
                    stage_outcomes_json,
                    evidence_refs_json,
                    final_outcome_class,
                    final_status,
                    head_sha,
                    created_at
                )
                VALUES (
                    :id,
                    :run_id,
                    :project_id,
                    :chunk_number,
                    :attempt_number,
                    :entry_mode,
                    :stage_outcomes_json,
                    :evidence_refs_json,
                    :final_outcome_class,
                    :final_status,
                    :head_sha,
                    :created_at
                )
            """),
            {
                "id": attempt_id,
                "run_id": run_id,
                "project_id": project_id,
                "chunk_number": chunk_number,
                "attempt_number": attempt_number,
                "entry_mode": entry_mode,
                "stage_outcomes_json": _json_dump(stage_outcomes or []),
                "evidence_refs_json": _json_dump(evidence_refs or []),
                "final_outcome_class": final_outcome_class,
                "final_status": final_status,
                "head_sha": head_sha,
                "created_at": created_at,
            },
        )
    return {
        "id": attempt_id,
        "run_id": run_id,
        "project_id": project_id,
        "chunk_number": chunk_number,
        "attempt_number": attempt_number,
        "entry_mode": entry_mode,
        "final_outcome_class": final_outcome_class,
        "final_status": final_status,
        "head_sha": head_sha,
        "created_at": created_at,
    }


def list_chunk_attempts(run_id: str, chunk_number: int | None = None) -> list[dict[str, Any]]:
    where = "WHERE run_id = :run_id"
    params: dict[str, Any] = {"run_id": run_id}
    if chunk_number is not None:
        where += " AND chunk_number = :chunk_number"
        params["chunk_number"] = chunk_number
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT *
                FROM chunk_attempts
                {where}
                ORDER BY chunk_number ASC, attempt_number ASC, created_at ASC
            """),
            params,
        ).mappings().all()
    return [dict(row) for row in rows]


def get_latest_completed_attempt_head(run_id: str) -> dict[str, Any] | None:
    """
    Return the newest completed chunk attempt with a recorded HEAD, if any.

    Resume uses this as a drift guard. A missing row means a pre-ledger or
    partially recorded run and intentionally degrades to the legacy behavior.

    Ordered by created_at first (item 14): a post-success refinement commits a
    *lower*-numbered chunk after higher ones already completed, so the actual
    branch HEAD is the most RECENTLY recorded completed attempt, not the one on
    the highest chunk number. Ordering by chunk_number first would return a stale
    head and brick resume on a perfectly healthy refined run. For monotonic
    (non-refined) runs created_at order equals the old chunk-number order, so the
    retained tiebreakers preserve the prior behavior exactly.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT *
                FROM chunk_attempts
                WHERE run_id = :run_id
                  AND final_status = 'completed'
                  AND head_sha IS NOT NULL
                  AND TRIM(head_sha) != ''
                ORDER BY created_at DESC, chunk_number DESC, attempt_number DESC
                LIMIT 1
            """),
            {"run_id": run_id},
        ).mappings().fetchone()
    return dict(row) if row is not None else None
