"""
llm_call_provenance_store.py
Isolated, additive persistence for METADATA-ONLY LLM call provenance
(#33B; docs/design/multi-provider-modes.md).

This is the dedicated home for "which provider/model actually produced a role's
effective output for a run/chunk". It is deliberately separate from checkpoints
(the resume/safety substrate) and from chunks/completion_summary, so provenance
never couples into the resume path or any patch-failure data.

Scope of this module: boring DB CRUD only. It performs NO LLM calls, NO git calls,
NO route calls, and NO repo mutation. It runs nothing, gates nothing, commits
nothing, retries nothing, and changes no approval, scope, or final-approval
behavior. Provenance is audit/display-only.

METADATA ONLY. This module stores provider/model names, the role, an optional
selection_source label, finish_reason, token counts, and audit ids/timestamps. It
must NEVER be passed (and never persists) prompts, LLM response text, diffs, file
contents, API keys/tokens/auth headers, or raw provider errors. The record model
below has no field capable of holding such content.
"""

import logging
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.llm.sanitize import sanitize_for_log

logger = logging.getLogger(__name__)


class LLMCallProvenanceRecord(BaseModel):
    """
    One metadata-only provenance row for a single role's effective LLM output.

    All content-bearing fields are intentionally absent: there is no place to put a
    prompt, a response, a diff, a key, or a raw error. ``provider``/``model`` are
    plain sanitized metadata names (never secrets).
    """

    # ``model`` is a plain metadata string; silence pydantic's protected-namespace
    # warning for the literal field name ``model``.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    run_id: str
    role: str
    provider: str
    model: str
    chunk_number: int | None = None
    # Reserved for a future slice; NOT derived here (deriving it would duplicate or
    # change resolve_role_config precedence, which is out of scope for #33B).
    selection_source: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    created_at: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row) -> LLMCallProvenanceRecord:
    """Rebuild a validated record from a stored row."""
    mapping = dict(row._mapping)
    created_at = mapping.get("created_at")
    return LLMCallProvenanceRecord.model_validate({
        "id": mapping["id"],
        "run_id": mapping["run_id"],
        "chunk_number": mapping.get("chunk_number"),
        "role": mapping["role"],
        "provider": mapping["provider"],
        "model": mapping["model"],
        "selection_source": mapping.get("selection_source"),
        "finish_reason": mapping.get("finish_reason"),
        "input_tokens": mapping.get("input_tokens"),
        "output_tokens": mapping.get("output_tokens"),
        "created_at": str(created_at) if created_at is not None else None,
    })


def record_llm_call_provenance(
    record: LLMCallProvenanceRecord,
) -> LLMCallProvenanceRecord:
    """
    Persist a single provenance row and return the stored row.

    Strict variant: raises RuntimeError on failure. Callers on a critical pipeline
    path should use ``try_record_llm_call_provenance`` instead, which never raises.
    ``id``/``created_at`` are generated here when blank.
    """
    row_id = record.id or str(uuid.uuid4())
    created_at = record.created_at or _now_iso()

    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO llm_call_provenance
                (
                    id, run_id, chunk_number, role, provider, model,
                    selection_source, finish_reason, input_tokens, output_tokens,
                    created_at
                )
                VALUES
                (
                    :id, :run_id, :chunk_number, :role, :provider, :model,
                    :selection_source, :finish_reason, :input_tokens,
                    :output_tokens, :created_at
                )
            """), {
                "id": row_id,
                "run_id": record.run_id,
                "chunk_number": record.chunk_number,
                "role": record.role,
                "provider": record.provider,
                "model": record.model,
                "selection_source": record.selection_source,
                "finish_reason": record.finish_reason,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "created_at": created_at,
            })
            row = conn.execute(text("""
                SELECT * FROM llm_call_provenance WHERE id = :id
            """), {"id": row_id}).fetchone()
            return _row_to_record(row)
    except Exception as error:
        raise RuntimeError(
            f"llm_call_provenance_store.py: record_llm_call_provenance failed. "
            f"run_id={record.run_id} | chunk_number={record.chunk_number} | "
            f"role={record.role} | error={sanitize_for_log(str(error))}"
        )


def try_record_llm_call_provenance(
    *,
    run_id: str,
    role: str,
    provider: str,
    model: str,
    chunk_number: int | None = None,
    selection_source: str | None = None,
    finish_reason: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> LLMCallProvenanceRecord | None:
    """
    Best-effort provenance write for use on a live pipeline path.

    This NEVER raises and NEVER changes the caller's outcome: any failure (bad
    input, DB error, anything) is swallowed and logged with a sanitized message,
    and ``None`` is returned. Provenance is advisory audit data — losing a row must
    never fail a chunk, a coder run, a patch, a commit, or an approval.
    """
    try:
        record = LLMCallProvenanceRecord(
            id=str(uuid.uuid4()),
            run_id=run_id,
            role=role,
            provider=provider,
            model=model,
            chunk_number=chunk_number,
            selection_source=selection_source,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return record_llm_call_provenance(record)
    except Exception as error:
        logger.warning(
            "[PROVENANCE] failed to record LLM call provenance (ignored) | "
            "run_id=%s | chunk=%s | role=%s | error=%s",
            run_id, chunk_number, role, sanitize_for_log(str(error)),
        )
        return None


def get_provenance_for_chunk(
    run_id: str,
    chunk_number: int | None,
    role: str | None = None,
) -> list[LLMCallProvenanceRecord]:
    """
    All provenance rows for (run, chunk), newest first; optionally filtered by role.
    Read-only. A ``None`` ``chunk_number`` matches rows whose chunk_number IS NULL.
    """
    init_db()
    clauses = ["run_id = :run_id"]
    params: dict = {"run_id": run_id}
    if chunk_number is None:
        clauses.append("chunk_number IS NULL")
    else:
        clauses.append("chunk_number = :chunk_number")
        params["chunk_number"] = chunk_number
    if role is not None:
        clauses.append("role = :role")
        params["role"] = role

    where = " AND ".join(clauses)
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT * FROM llm_call_provenance WHERE {where} "
            "ORDER BY created_at DESC, id DESC"
        ), params).fetchall()
    return [_row_to_record(row) for row in rows]
