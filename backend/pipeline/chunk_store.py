"""
chunk_store.py
Persistence helpers for Phase 2B chunk plans.

This module stores and reads chunk plans. It does not execute chunks.
"""

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from backend.core.status_service import update_chunk_status as _service_update_chunk_status
from backend.core.statuses import ChunkPlanStatus, ChunkStatusValue, RunStatus
from backend.db.database import engine, init_db
from backend.models.chunk import (
    ChunkPlanResponse,
    ChunkStatus,
    TriageResult,
)


def _json_loads_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _chunk_row_to_status(row) -> ChunkStatus:
    data = dict(row._mapping)
    return ChunkStatus(
        run_id=data["run_id"],
        project_id=data["project_id"],
        chunk_number=data["chunk_number"],
        title=data["title"],
        status=data["status"],
        risk_level=data["risk_level"],
        requires_human_review=bool(data["requires_human_review"]),
        files_expected=_json_loads_list(data.get("files_expected")),
        depends_on=_json_loads_list(data.get("depends_on")),
        completion_summary=data.get("completion_summary"),
        error_message=data.get("error_message"),
    )


def _load_run(conn, run_id: str):
    return conn.execute(text("""
        SELECT * FROM pipeline_runs
        WHERE id = :run_id
    """), {"run_id": run_id}).fetchone()


def _load_chunk_rows(conn, run_id: str):
    return conn.execute(text("""
        SELECT * FROM chunks
        WHERE run_id = :run_id
        ORDER BY chunk_number ASC
    """), {"run_id": run_id}).fetchall()


def _insert_chunks(conn, run_id: str, project_id: str, triage_result: TriageResult) -> int:
    for chunk in triage_result.chunks:
        conn.execute(text("""
            INSERT OR IGNORE INTO chunks
            (
                id, run_id, project_id, chunk_number, title, description,
                files_expected, depends_on, risk_level, token_estimate,
                requires_human_review, status
            )
            VALUES
            (
                :id, :run_id, :project_id, :chunk_number, :title,
                :description, :files_expected, :depends_on, :risk_level,
                :token_estimate, :requires_human_review, :status
            )
        """), {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "project_id": project_id,
            "chunk_number": chunk.chunk_number,
            "title": chunk.title,
            "description": chunk.description,
            "files_expected": json.dumps(chunk.files_expected),
            "depends_on": json.dumps(chunk.depends_on),
            "risk_level": chunk.risk_level,
            "token_estimate": chunk.token_estimate,
            "requires_human_review": 1 if chunk.requires_human_review else 0,
            "status": ChunkStatusValue.PENDING,
        })

    row = conn.execute(text("""
        SELECT COUNT(*) FROM chunks
        WHERE run_id = :run_id
    """), {"run_id": run_id}).fetchone()
    return int(row[0])


def create_chunked_run(
    run_id: str,
    project_id: str,
    feature_description: str,
    triage_result: TriageResult,
    *,
    intent: str | None = None,
    source_plan_run_id: str | None = None,
) -> ChunkPlanResponse:
    """
    Create one parent pipeline run and its proposed chunks transactionally.

    ``intent`` and ``source_plan_run_id`` are optional. ``source_plan_run_id``
    is set when this run is derived from a prior plan_only run via the
    plan-to-implementation handoff, so it can be looked up for idempotency.
    """
    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_runs
                (
                    id, project_id, feature_description, status,
                    current_step, intent, chunk_plan_status, chunk_plan,
                    total_chunks, current_chunk_number,
                    source_plan_run_id, created_at
                )
                VALUES
                (
                    :id, :project_id, :feature_description,
                    :status, 'chunk_plan', :intent,
                    :chunk_plan_status, :chunk_plan, :total_chunks,
                    0, :source_plan_run_id, :created_at
                )
            """), {
                "id": run_id,
                "project_id": project_id,
                "feature_description": feature_description,
                "status": RunStatus.AWAITING_CHUNK_PLAN_APPROVAL,
                "intent": intent or "implementation",
                "chunk_plan_status": ChunkPlanStatus.AWAITING_APPROVAL,
                "chunk_plan": triage_result.model_dump_json(),
                "total_chunks": triage_result.total_chunks,
                "source_plan_run_id": source_plan_run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            _insert_chunks(conn, run_id, project_id, triage_result)

        print(f"[CHUNKS] Chunked run created | run_id={run_id}")
        return get_chunk_plan_status(run_id)
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: create_chunked_run failed. "
            f"run_id={run_id} | error={error}"
        )


def save_chunks_to_db(
    run_id: str,
    project_id: str,
    triage_result: TriageResult,
) -> int:
    """
    Save chunk rows idempotently for an existing run.
    """
    try:
        init_db()
        with engine.begin() as conn:
            count = _insert_chunks(conn, run_id, project_id, triage_result)
        print(f"[CHUNKS] Chunks saved | run_id={run_id} | count={count}")
        return count
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: save_chunks_to_db failed. "
            f"run_id={run_id} | error={error}"
        )


def get_chunk_plan_status(run_id: str) -> ChunkPlanResponse:
    try:
        init_db()
        with engine.connect() as conn:
            run = _load_run(conn, run_id)
            if run is None:
                raise ValueError(f"chunk_store.py: run not found: {run_id}")
            chunks = [_chunk_row_to_status(row) for row in _load_chunk_rows(conn, run_id)]

        run_data = dict(run._mapping)
        triage = None
        if run_data.get("chunk_plan"):
            triage = TriageResult.model_validate_json(run_data["chunk_plan"])

        return ChunkPlanResponse(
            run_id=run_id,
            project_id=run_data.get("project_id") or "",
            chunk_plan_status=run_data.get("chunk_plan_status") or ChunkPlanStatus.NONE,
            total_chunks=run_data.get("total_chunks") or len(chunks),
            current_chunk_number=run_data.get("current_chunk_number") or 0,
            triage=triage,
            chunks=chunks,
        )
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_chunk_plan_status failed. "
            f"run_id={run_id} | error={error}"
        )


def _require_awaiting_approval(conn, run_id: str):
    run = _load_run(conn, run_id)
    if run is None:
        raise ValueError(f"chunk_store.py: run not found: {run_id}")

    status = dict(run._mapping).get("chunk_plan_status")
    if status != ChunkPlanStatus.AWAITING_APPROVAL:
        raise RuntimeError(
            f"chunk_store.py: chunk plan is not awaiting approval. "
            f"run_id={run_id} | status={status}"
        )
    return run


def approve_chunk_plan(run_id: str) -> ChunkPlanResponse:
    try:
        init_db()
        with engine.begin() as conn:
            _require_awaiting_approval(conn, run_id)
            conn.execute(text("""
                UPDATE pipeline_runs
                SET chunk_plan_status = :chunk_plan_status,
                    status = :status,
                    current_step = :current_step
                WHERE id = :run_id
            """), {
                "run_id": run_id,
                "chunk_plan_status": ChunkPlanStatus.APPROVED,
                "status": RunStatus.CHUNK_PLAN_APPROVED,
                "current_step": RunStatus.CHUNK_PLAN_APPROVED,
            })
        print(f"[CHUNKS] Chunk plan approved | run_id={run_id}")
        return get_chunk_plan_status(run_id)
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: approve_chunk_plan failed. "
            f"run_id={run_id} | error={error}"
        )


def reject_chunk_plan(
    run_id: str,
    reason: str | None = None,
) -> ChunkPlanResponse:
    try:
        init_db()
        with engine.begin() as conn:
            _require_awaiting_approval(conn, run_id)
            conn.execute(text("""
                UPDATE pipeline_runs
                SET chunk_plan_status = :chunk_plan_status,
                    status = :status,
                    current_step = 'chunk_plan_rejected',
                    plain_english_summary = :reason
                WHERE id = :run_id
            """), {
                "run_id": run_id,
                "chunk_plan_status": ChunkPlanStatus.REJECTED,
                "status": RunStatus.REJECTED,
                "reason": reason or "Chunk plan rejected.",
            })
        print(f"[CHUNKS] Chunk plan rejected | run_id={run_id}")
        return get_chunk_plan_status(run_id)
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: reject_chunk_plan failed. "
            f"run_id={run_id} | error={error}"
        )


def get_pending_chunks(run_id: str) -> list[ChunkStatus]:
    try:
        init_db()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM chunks
                WHERE run_id = :run_id AND status = :status
                ORDER BY chunk_number ASC
            """), {
                "run_id": run_id,
                "status": ChunkStatusValue.PENDING,
            }).fetchall()
        return [_chunk_row_to_status(row) for row in rows]
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_pending_chunks failed. "
            f"run_id={run_id} | error={error}"
        )


def get_chunk(run_id: str, chunk_number: int) -> ChunkStatus:
    try:
        init_db()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM chunks
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            }).fetchone()
        if row is None:
            raise ValueError(
                f"chunk_store.py: chunk not found. "
                f"run_id={run_id} | chunk_number={chunk_number}"
            )
        return _chunk_row_to_status(row)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_chunk failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def update_chunk_status(
    run_id: str,
    chunk_number: int,
    status: str,
    error_message: str | None = None,
) -> None:
    _service_update_chunk_status(run_id, chunk_number, status, error_message)


def save_chunk_completion_summary(
    run_id: str,
    chunk_number: int,
    summary: dict | str,
) -> None:
    try:
        summary_text = json.dumps(summary) if isinstance(summary, dict) else summary
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE chunks
                SET completion_summary = :summary,
                    completed_at = :completed_at
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "summary": summary_text,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "chunk_number": chunk_number,
            })
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: save_chunk_completion_summary failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def _format_summary_field(summary: dict, key: str) -> str:
    value = summary.get(key)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    if value:
        return str(value)
    return "none"


def _format_completed_chunk(row) -> str:
    data = dict(row._mapping)
    raw_summary = data.get("completion_summary")

    if raw_summary is None:
        return (
            f"Chunk {data['chunk_number']} - {data['title']} [COMPLETE]\n"
            f"Files created: none\n"
            f"Files modified: none\n"
            f"Key decisions: none\n"
            f"Tests added: none\n"
            f"Summary: not available (crash recovery)"
        )

    try:
        parsed = json.loads(raw_summary)
        if isinstance(parsed, dict):
            return (
                f"Chunk {data['chunk_number']} - {data['title']} [COMPLETE]\n"
                f"Files created: {_format_summary_field(parsed, 'files_created')}\n"
                f"Files modified: {_format_summary_field(parsed, 'files_modified')}\n"
                f"Key decisions: {_format_summary_field(parsed, 'key_decisions')}\n"
                f"Tests added: {_format_summary_field(parsed, 'tests_added')}\n"
                f"Summary: {_format_summary_field(parsed, 'summary')}"
            )
    except Exception:
        pass

    return (
        f"Chunk {data['chunk_number']} - {data['title']} [COMPLETE]\n"
        f"Files created: none\n"
        f"Files modified: none\n"
        f"Key decisions: none\n"
        f"Tests added: none\n"
        f"Summary: {raw_summary}"
    )


def get_previous_chunks_context(run_id: str, before_chunk_number: int) -> str:
    try:
        init_db()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM chunks
                WHERE run_id = :run_id
                  AND chunk_number < :before_chunk_number
                  AND status = :status
                ORDER BY chunk_number ASC
            """), {
                "run_id": run_id,
                "before_chunk_number": before_chunk_number,
                "status": ChunkStatusValue.COMPLETED,
            }).fetchall()

        parts = ["[Previous Chunks Context]"]
        for row in rows:
            parts.append("")
            parts.append(_format_completed_chunk(row))
        parts.append("")
        parts.append("[End Previous Context]")
        context = "\n".join(parts)
        return context[:6000]
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_previous_chunks_context failed. "
            f"run_id={run_id} | before_chunk_number={before_chunk_number} | "
            f"error={error}"
        )
