"""
plan_version_store.py

Append-only plan version scaffold for §23 rows 7a/7b.

triage_json is plan data: the same serialized TriageResult already stored in
pipeline_runs.chunk_plan, with the same trust class and safety posture. This
scaffold records version 1 at plan creation time and later plan-turn versions
while a run is still awaiting plan approval. It does not store diffs, repo file
contents, prompts, provider output, test output, or secrets beyond what the
existing plan JSON already contains, and runtime behavior continues to read
pipeline_runs.chunk_plan as the live plan pointer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.db.database import engine

PLAN_VERSION_SOURCE_INITIAL = "initial"
PLAN_VERSION_SOURCE_SEEDED = "seeded"
PLAN_VERSION_SOURCE_PLAN_TURN = "plan_turn"
PLAN_VERSION_SOURCES = {
    PLAN_VERSION_SOURCE_INITIAL,
    PLAN_VERSION_SOURCE_SEEDED,
    PLAN_VERSION_SOURCE_PLAN_TURN,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_plan_version(
    conn: Connection,
    *,
    run_id: str,
    version: int,
    triage_json: str,
    source: str,
    created_from_turn_id: str | None = None,
) -> dict[str, Any]:
    """
    Append one plan_versions row using the caller's transaction.

    Callers must pass the same connection used to create/update the owning run
    so a plan-bearing run cannot commit without its matching version row.
    """
    if version < 1:
        raise ValueError("plan_version_store.py: version must be >= 1")
    if source not in PLAN_VERSION_SOURCES:
        raise ValueError(
            f"plan_version_store.py: unsupported plan version source: {source}"
        )

    plan_version_id = str(uuid.uuid4())
    created_at = _utc_now()
    conn.execute(
        text("""
            INSERT INTO plan_versions (
                id,
                run_id,
                version,
                triage_json,
                source,
                created_from_turn_id,
                created_at
            )
            VALUES (
                :id,
                :run_id,
                :version,
                :triage_json,
                :source,
                :created_from_turn_id,
                :created_at
            )
        """),
        {
            "id": plan_version_id,
            "run_id": run_id,
            "version": version,
            "triage_json": triage_json,
            "source": source,
            "created_from_turn_id": created_from_turn_id,
            "created_at": created_at,
        },
    )
    return {
        "id": plan_version_id,
        "run_id": run_id,
        "version": version,
        "triage_json": triage_json,
        "source": source,
        "created_from_turn_id": created_from_turn_id,
        "created_at": created_at,
    }


def list_plan_versions(run_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT *
                FROM plan_versions
                WHERE run_id = :run_id
                ORDER BY version ASC, created_at ASC, id ASC
            """),
            {"run_id": run_id},
        ).mappings().all()
    return [dict(row) for row in rows]
