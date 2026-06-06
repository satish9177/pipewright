"""
injection_store.py
Append-only persistence for memory injection provenance (M3C1;
docs/design/memory-m3-trust-lifecycle.md §14).

This is the dedicated home for "which approved memory facts were injected into a
role's prompt for a run/chunk at execution time". It exists so a human can later
audit memory influence (e.g. wrong-memory / poisoning) even after the underlying
facts are edited, archived, marked stale, or superseded.

Scope of this module: boring DB CRUD only. It performs NO LLM calls, NO git
calls, NO repo scans, NO route calls, and NO memory mutation. It runs nothing,
gates nothing, commits nothing, retries nothing, and changes no approval, scope,
final-approval, or prompt behavior. Provenance is audit/display-only and
APPEND-ONLY: rows are never updated or deleted here.

MEMORY ENTRIES ONLY. The captured content is the already-approved memory facts a
role received — never the full prompt, repo files, handoff contracts, source
code, API keys, or tokens. As defense-in-depth, captured content is re-run
through the same write-path safety gate (validate_memory_content); anything that
somehow fails is redacted rather than stored verbatim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.llm.sanitize import sanitize_for_log
from backend.memory.memory_store import validate_memory_content

logger = logging.getLogger(__name__)

_REDACTED = "[redacted: failed memory safety re-validation]"

# Keys persisted per captured entry. content_hash is stored for drift/integrity
# checks; the read endpoint decides whether to expose it.
_ENTRY_KEYS = (
    "fact_id",
    "content",
    "content_hash",
    "category",
    "scope",
    "priority",
    "status_at_injection",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry_to_dict(entry) -> dict:
    """Normalize an entry (dataclass or mapping) to a plain dict of _ENTRY_KEYS."""
    if isinstance(entry, dict):
        get = entry.get
    else:
        def get(key, default=None):
            return getattr(entry, key, default)
    return {key: get(key) for key in _ENTRY_KEYS}


def _sanitize_entry(entry) -> dict:
    """
    Normalize and re-validate one entry's content as defense-in-depth. If the
    content does not pass the write-path safety gate (should be impossible for an
    already-approved active fact), redact the content but keep the metadata so
    the event still records that something was injected.
    """
    data = _entry_to_dict(entry)
    content = data.get("content")
    try:
        validate_memory_content(content if isinstance(content, str) else "")
    except Exception:
        data["content"] = _REDACTED
    return data


def _sanitize_entries(entries) -> list[dict]:
    return [_sanitize_entry(entry) for entry in (entries or [])]


def compute_entries_hash(included_entries) -> str:
    """
    Deterministic digest of the ORDERED included entries. Hashes only the entry
    identity/content fields — never the timestamped block header — so two
    injections of the same facts in the same order produce the same hash.
    """
    canonical = "\n".join(
        "|".join((
            str(entry.get("fact_id") or ""),
            str(entry.get("content_hash") or ""),
            str(entry.get("category") or ""),
            str(entry.get("scope") or ""),
            str(entry.get("content") or ""),
        ))
        for entry in (included_entries or [])
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _next_attempt_number(
    conn, run_id: str, project_id: str, role: str, chunk_number: int | None
) -> int:
    """
    Conservative monotonic attempt counter per (run, project, role, chunk).

    This intentionally does NOT touch the patch-failure/retry machinery; it just
    counts existing provenance rows for the same role invocation slot and adds 1,
    so a coder re-run (patch retry, scope-expansion retry) naturally lands as a
    higher attempt_number without any refactor. attempt_id remains a separate,
    nullable field that a later slice may wire to the patch-failure attempt id.
    """
    clauses = [
        "run_id = :run_id",
        "project_id = :project_id",
        "role = :role",
    ]
    params: dict = {"run_id": run_id, "project_id": project_id, "role": role}
    if chunk_number is None:
        clauses.append("chunk_number IS NULL")
    else:
        clauses.append("chunk_number = :chunk_number")
        params["chunk_number"] = chunk_number
    where = " AND ".join(clauses)
    existing = conn.execute(
        text(f"SELECT COUNT(*) FROM memory_injection_events WHERE {where}"),
        params,
    ).scalar()
    return int(existing or 0) + 1


def _row_to_dict(row) -> dict:
    mapping = dict(row._mapping)
    try:
        entries = json.loads(mapping.get("entries_json") or "{}")
    except Exception:
        entries = {}
    try:
        category_policy = json.loads(mapping.get("category_policy") or "[]")
    except Exception:
        category_policy = []
    created_at = mapping.get("created_at")
    return {
        "id": mapping["id"],
        "run_id": mapping["run_id"],
        "project_id": mapping["project_id"],
        "chunk_number": mapping.get("chunk_number"),
        "role": mapping["role"],
        "attempt_number": mapping.get("attempt_number"),
        "attempt_id": mapping.get("attempt_id"),
        "repo_head_sha": mapping.get("repo_head_sha"),
        "token_budget": mapping.get("token_budget"),
        "category_policy": category_policy if isinstance(category_policy, list) else [],
        "included_entries": entries.get("included", []) if isinstance(entries, dict) else [],
        "excluded_entries": entries.get("excluded", []) if isinstance(entries, dict) else [],
        "included_count": mapping.get("included_count"),
        "excluded_count": mapping.get("excluded_count"),
        "entries_hash": mapping.get("entries_hash"),
        "created_at": str(created_at) if created_at is not None else None,
    }


def record_memory_injection_event(
    *,
    run_id: str,
    project_id: str,
    role: str,
    token_budget: int | None,
    category_policy,
    included_entries,
    excluded_entries=None,
    chunk_number: int | None = None,
    attempt_number: int | None = None,
    attempt_id: str | None = None,
    repo_head_sha: str | None = None,
) -> dict:
    """
    Persist one append-only memory injection event and return the stored row.

    Strict variant: raises ValueError on bad input and RuntimeError on DB error.
    Callers on a live pipeline path should use capture_memory_injection, which
    never raises. project_id/run_id/role are required and project-scoped.
    """
    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("injection_store.py: project_id is required")
    rid = (run_id or "").strip()
    if not rid:
        raise ValueError("injection_store.py: run_id is required")
    role_value = (role or "").strip()
    if not role_value:
        raise ValueError("injection_store.py: role is required")

    included = _sanitize_entries(included_entries)
    excluded = _sanitize_entries(excluded_entries)
    entries_hash = compute_entries_hash(included)
    entries_json = json.dumps({"included": included, "excluded": excluded})
    category_policy_json = json.dumps(list(category_policy or []))
    row_id = str(uuid.uuid4())
    created_at = _now_iso()

    try:
        init_db()
        with engine.begin() as conn:
            resolved_attempt = (
                attempt_number
                if attempt_number is not None
                else _next_attempt_number(conn, rid, pid, role_value, chunk_number)
            )
            conn.execute(text("""
                INSERT INTO memory_injection_events
                (
                    id, run_id, project_id, chunk_number, role, attempt_number,
                    attempt_id, repo_head_sha, token_budget, category_policy,
                    entries_json, included_count, excluded_count, entries_hash,
                    created_at
                )
                VALUES
                (
                    :id, :run_id, :project_id, :chunk_number, :role,
                    :attempt_number, :attempt_id, :repo_head_sha, :token_budget,
                    :category_policy, :entries_json, :included_count,
                    :excluded_count, :entries_hash, :created_at
                )
            """), {
                "id": row_id,
                "run_id": rid,
                "project_id": pid,
                "chunk_number": chunk_number,
                "role": role_value,
                "attempt_number": resolved_attempt,
                "attempt_id": attempt_id,
                "repo_head_sha": repo_head_sha,
                "token_budget": token_budget,
                "category_policy": category_policy_json,
                "entries_json": entries_json,
                "included_count": len(included),
                "excluded_count": len(excluded),
                "entries_hash": entries_hash,
                "created_at": created_at,
            })
            row = conn.execute(text("""
                SELECT * FROM memory_injection_events WHERE id = :id
            """), {"id": row_id}).fetchone()
            return _row_to_dict(row)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"injection_store.py: record_memory_injection_event failed. "
            f"run_id={rid} | chunk_number={chunk_number} | role={role_value} | "
            f"error={sanitize_for_log(str(error))}"
        )


def capture_memory_injection(
    result,
    *,
    run_id: str,
    project_id: str | None,
    role: str,
    chunk_number: int | None = None,
    attempt_number: int | None = None,
    attempt_id: str | None = None,
    repo_head_sha: str | None = None,
) -> dict | None:
    """
    Best-effort capture for use on a live pipeline path.

    NEVER raises and NEVER changes the caller's outcome: any failure (bad input,
    DB error, anything) is swallowed and logged with a sanitized message, and
    None is returned. Losing a provenance row must never fail a run, a chunk, a
    coder/planner/triage call, a patch, a commit, or an approval.

    ``result`` is a MemoryBlockBuildResult (duck-typed): only its memory entries
    and policy metadata are read — never a prompt or repo content.
    """
    try:
        included = list(getattr(result, "included_entries", ()) or ())
        excluded = list(getattr(result, "excluded_entries", ()) or ())
        return record_memory_injection_event(
            run_id=run_id,
            project_id=project_id or "",
            role=role,
            token_budget=getattr(result, "token_budget", None),
            category_policy=list(getattr(result, "category_policy", ()) or ()),
            included_entries=included,
            excluded_entries=excluded,
            chunk_number=chunk_number,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            repo_head_sha=repo_head_sha,
        )
    except Exception as error:
        logger.warning(
            "[MEMORY-PROVENANCE] failed to record injection event (ignored) | "
            "run_id=%s | chunk=%s | role=%s | error=%s",
            run_id, chunk_number, role, sanitize_for_log(str(error)),
        )
        return None


def list_memory_injection_events(
    run_id: str,
    *,
    project_id: str | None = None,
    chunk_number: int | None = None,
    role: str | None = None,
) -> list[dict]:
    """
    All memory injection events for a run, newest first. Read-only. Optionally
    filtered by project_id (scope), chunk_number, and role. An omitted filter
    means "no filter" (unlike a None chunk_number, which is a stored value for
    run-level roles and is only matched when no chunk_number filter is given).
    """
    init_db()
    clauses = ["run_id = :run_id"]
    params: dict = {"run_id": run_id}
    if project_id is not None:
        clauses.append("project_id = :project_id")
        params["project_id"] = project_id
    if chunk_number is not None:
        clauses.append("chunk_number = :chunk_number")
        params["chunk_number"] = chunk_number
    if role is not None:
        clauses.append("role = :role")
        params["role"] = role
    where = " AND ".join(clauses)
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT * FROM memory_injection_events WHERE {where} "
            "ORDER BY created_at DESC, id DESC"
        ), params).fetchall()
    return [_row_to_dict(row) for row in rows]
