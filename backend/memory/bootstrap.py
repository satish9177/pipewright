"""
bootstrap.py
Deterministic project memory bootstrap suggestions.

Suggestions are proposals only. They become active memory only after a human
approves them through the Memory API.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.db.database import engine
from backend.memory.memory_store import (
    DEFAULT_PRIORITY,
    MEMORY_DUPLICATE_ERROR,
    compute_content_hash,
    insert_fact_in_conn,
    supersede_fact_in_conn,
    validate_fact_fields,
    validate_memory_content,
)
from backend.projects.project_store import get_project
from backend.repo.repo_fingerprint import (
    IGNORED_DIRS,
    MAX_DEPTH,
    MAX_MANIFEST_FILES,
    discover_manifest_files,
    load_repo_file,
)

logger = logging.getLogger(__name__)

ALLOWED_SUGGESTION_STATUSES = {"pending", "approved", "rejected", "archived"}
BOOTSTRAP_SOURCE = "bootstrap"

# Caps remain bootstrap-local module constants so existing tests can monkeypatch
# bootstrap.BOOTSTRAP_MAX_MANIFEST_FILES; their default values come from the
# shared repo_fingerprint module so there is one source of truth. The discovery
# wrapper below reads these at call time, preserving the monkeypatch behavior.
BOOTSTRAP_MAX_DEPTH = MAX_DEPTH
BOOTSTRAP_MAX_MANIFEST_FILES = MAX_MANIFEST_FILES

# Skip list and manifest sets are now owned by repo_fingerprint (single source of
# truth). Re-exported under the historical bootstrap name for compatibility.
BOOTSTRAP_IGNORED_DIRS = IGNORED_DIRS


@dataclass(frozen=True)
class CandidateSuggestion:
    content: str
    category: str
    scope: str
    priority: int = DEFAULT_PRIORITY
    evidence_path: str | None = None
    evidence_excerpt: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row._mapping)


def _sanitize_suggestion(row: dict) -> dict:
    return {
        key: value
        for key, value in row.items()
        if key != "content_hash"
    }


def _sanitize_fact(row: dict) -> dict:
    return {
        key: value
        for key, value in row.items()
        if key != "content_hash"
    }


def _load_repo_file(root: Path, relative_path: str) -> str | None:
    return load_repo_file(root, relative_path)


def _discover_manifest_files(root: Path) -> list[str]:
    # Reads bootstrap's module-level caps at call time so existing tests that
    # monkeypatch BOOTSTRAP_MAX_MANIFEST_FILES continue to take effect.
    return discover_manifest_files(
        root,
        ignored_dirs=BOOTSTRAP_IGNORED_DIRS,
        max_depth=BOOTSTRAP_MAX_DEPTH,
        max_files=BOOTSTRAP_MAX_MANIFEST_FILES,
    )


def _lower_files(files: dict[str, str]) -> dict[str, str]:
    return {path: content.lower() for path, content in files.items()}


def _collect_candidates(root: Path) -> list[CandidateSuggestion]:
    files = {
        relative_path: content
        for relative_path in _discover_manifest_files(root)
        if (content := _load_repo_file(root, relative_path)) is not None
    }
    lowered = _lower_files(files)
    from backend.memory.detection_rules import collect_detection_candidates

    return collect_detection_candidates(root=root, files=files, lowered=lowered)


def _active_memory_exists(project_id: str, content_hash: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM memory_facts
            WHERE project_id = :project_id
              AND content_hash = :content_hash
              AND status = 'active'
            LIMIT 1
        """), {
            "project_id": project_id,
            "content_hash": content_hash,
        }).fetchone()
    return row is not None


def _pending_suggestion_exists(project_id: str, content_hash: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM memory_suggestions
            WHERE project_id = :project_id
              AND content_hash = :content_hash
              AND status = 'pending'
            LIMIT 1
        """), {
            "project_id": project_id,
            "content_hash": content_hash,
        }).fetchone()
    return row is not None


def _rejected_suggestion_exists(project_id: str, content_hash: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM memory_suggestions
            WHERE project_id = :project_id
              AND content_hash = :content_hash
              AND status = 'rejected'
            LIMIT 1
        """), {
            "project_id": project_id,
            "content_hash": content_hash,
        }).fetchone()
    return row is not None


def _run_scoped_suggestion_exists(
    project_id: str, content_hash: str, source_run_id: str
) -> bool:
    """
    True if this run already produced a suggestion with the same content,
    whether it is still pending, was approved, or was rejected.

    Project-scoped active/pending/rejected content-hash dedupe handles most
    cases. This preserves the older run-scoped guard for statuses tied to one
    source run, including approved/rejected rows that should not be recreated
    by repeating generation for that same run.
    """
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM memory_suggestions
            WHERE project_id = :project_id
              AND content_hash = :content_hash
              AND source_run_id = :source_run_id
              AND status IN ('pending', 'approved', 'rejected')
            LIMIT 1
        """), {
            "project_id": project_id,
            "content_hash": content_hash,
            "source_run_id": source_run_id,
        }).fetchone()
    return row is not None


_SUGGESTION_COLUMNS = """
    id, project_id, content, category, scope, priority, source,
    evidence_path, evidence_excerpt, status, created_at,
    updated_at, approved_by, approved_at, rejected_by,
    rejected_at, rejection_reason, content_hash,
    edited_content, approved_fact_id,
    source_run_id, source_chunk_number, source_type, source_ref,
    rationale, suggested_by, risk_level, quality_score
"""


def _get_suggestion_row(conn, project_id: str, suggestion_id: str) -> dict | None:
    row = conn.execute(text(f"""
        SELECT {_SUGGESTION_COLUMNS}
        FROM memory_suggestions
        WHERE project_id = :project_id
          AND id = :suggestion_id
    """), {
        "project_id": project_id,
        "suggestion_id": suggestion_id,
    }).fetchone()
    return _row_to_dict(row)


def _get_suggestion(project_id: str, suggestion_id: str) -> dict | None:
    with engine.connect() as conn:
        return _get_suggestion_row(conn, project_id, suggestion_id)


def _insert_suggestion(project_id: str, candidate: CandidateSuggestion) -> dict:
    content = validate_memory_content(candidate.content)
    content_hash = compute_content_hash(content)
    suggestion_id = str(uuid.uuid4())
    now = _utc_now()
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO memory_suggestions
            (id, project_id, content, category, scope, priority, source,
             evidence_path, evidence_excerpt, status, created_at, updated_at,
             content_hash)
            VALUES
            (:id, :project_id, :content, :category, :scope, :priority,
             :source, :evidence_path, :evidence_excerpt, 'pending', :now,
             :now, :content_hash)
        """), {
            "id": suggestion_id,
            "project_id": project_id,
            "content": content,
            "category": candidate.category,
            "scope": candidate.scope,
            "priority": candidate.priority,
            "source": BOOTSTRAP_SOURCE,
            "evidence_path": candidate.evidence_path,
            "evidence_excerpt": candidate.evidence_excerpt,
            "now": now,
            "content_hash": content_hash,
        })
        conn.commit()
    return _sanitize_suggestion(_get_suggestion(project_id, suggestion_id) or {})


def insert_pending_suggestion(
    project_id: str,
    content: str,
    *,
    category: str = "other",
    scope: str = "global",
    priority: int = DEFAULT_PRIORITY,
    source: str = "run_outcome",
    source_type: str | None = None,
    source_run_id: str | None = None,
    source_chunk_number: int | None = None,
    source_ref: str | None = None,
    rationale: str | None = None,
    suggested_by: str | None = None,
    risk_level: str | None = None,
    quality_score: int | None = None,
    evidence_path: str | None = None,
    evidence_excerpt: str | None = None,
) -> dict | None:
    """
    Validate and insert a single pending suggestion with provenance fields.

    Reuses the same content gate as manual memory creation
    (validate_memory_content, which raises ValueError on blocked content) and
    the same per-project dedupe (active fact + pending/rejected suggestion
    content hash).

    Returns the sanitized suggestion dict, or None when an active fact, pending
    suggestion, or rejected suggestion with the same content hash already
    exists. Suggestions are always inserted as pending — this never creates an
    active memory fact.
    """
    content = validate_memory_content(content)
    content_hash = compute_content_hash(content)
    if _active_memory_exists(project_id, content_hash):
        return None
    if _pending_suggestion_exists(project_id, content_hash):
        return None
    if _rejected_suggestion_exists(project_id, content_hash):
        return None
    # Run-scoped suppression: once this run produced this suggestion, do not
    # recreate it for the same run even if it was rejected (or already approved).
    if source_run_id and _run_scoped_suggestion_exists(
        project_id, content_hash, source_run_id
    ):
        return None

    suggestion_id = str(uuid.uuid4())
    now = _utc_now()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO memory_suggestions
                (id, project_id, content, category, scope, priority, source,
                 evidence_path, evidence_excerpt, status, created_at, updated_at,
                 content_hash, source_type, source_run_id, source_chunk_number,
                 source_ref, rationale, suggested_by, risk_level, quality_score)
                VALUES
                (:id, :project_id, :content, :category, :scope, :priority,
                 :source, :evidence_path, :evidence_excerpt, 'pending', :now,
                 :now, :content_hash, :source_type, :source_run_id,
                 :source_chunk_number, :source_ref, :rationale, :suggested_by,
                 :risk_level, :quality_score)
            """), {
                "id": suggestion_id,
                "project_id": project_id,
                "content": content,
                "category": category,
                "scope": scope,
                "priority": priority,
                "source": source,
                "evidence_path": evidence_path,
                "evidence_excerpt": evidence_excerpt,
                "now": now,
                "content_hash": content_hash,
                "source_type": source_type,
                "source_run_id": source_run_id,
                "source_chunk_number": source_chunk_number,
                "source_ref": source_ref,
                "rationale": rationale,
                "suggested_by": suggested_by,
                "risk_level": risk_level,
                "quality_score": quality_score,
            })
    except IntegrityError:
        # Lost a race against the partial pending-dedupe unique index.
        return None
    return _sanitize_suggestion(_get_suggestion(project_id, suggestion_id) or {})


def generate_bootstrap_suggestions(project_id: str, force: bool = False) -> list[dict]:
    project = get_project(project_id)
    if project is None:
        raise ValueError("bootstrap.py: project not found")

    repo_path = project.get("repo_path")
    if not repo_path:
        raise RuntimeError("bootstrap.py: project repo_path is missing")
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise RuntimeError("bootstrap.py: project repo_path is invalid")

    created: list[dict] = []
    seen_hashes: set[str] = set()
    for candidate in _collect_candidates(root):
        try:
            content = validate_memory_content(candidate.content)
            content_hash = compute_content_hash(content)
        except ValueError:
            logger.warning("bootstrap.py: skipped unsafe memory suggestion")
            continue
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        if _active_memory_exists(project_id, content_hash):
            continue
        if _pending_suggestion_exists(project_id, content_hash):
            continue
        try:
            created.append(_insert_suggestion(project_id, candidate))
        except IntegrityError:
            if force:
                continue
            continue
    return created


def list_suggestions(project_id: str, status: str | None = None) -> list[dict]:
    if status is not None and status not in ALLOWED_SUGGESTION_STATUSES:
        raise ValueError("bootstrap.py: invalid suggestion status")

    filters = ["project_id = :project_id"]
    params = {"project_id": project_id}
    if status is not None:
        filters.append("status = :status")
        params["status"] = status

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT {_SUGGESTION_COLUMNS}
            FROM memory_suggestions
            WHERE {" AND ".join(filters)}
            ORDER BY created_at DESC
        """), params).fetchall()
    return [_sanitize_suggestion(dict(row._mapping)) for row in rows]


def _approve_suggestion_in_conn(
    conn,
    *,
    project_id: str,
    suggestion_id: str,
    approved_by: str = "api",
    edited_content: str | None = None,
    now: str | None = None,
) -> dict:
    edited = edited_content.strip() if edited_content and edited_content.strip() else None
    now = now or _utc_now()
    suggestion = _get_suggestion_row(conn, project_id, suggestion_id)
    if suggestion is None:
        raise ValueError("bootstrap.py: suggestion not found")
    if suggestion["status"] != "pending":
        raise ValueError("bootstrap.py: suggestion is not pending")

    fact_content = edited if edited is not None else suggestion["content"]
    fact_content, category, scope, priority = validate_fact_fields(
        fact_content,
        suggestion["category"],
        suggestion["scope"],
        suggestion["priority"],
    )

    fact = insert_fact_in_conn(
        conn,
        project_id=project_id,
        content=fact_content,
        category=category,
        scope=scope,
        priority=priority,
        source=suggestion["source"] or BOOTSTRAP_SOURCE,
        added_by="api",
        approved_by=approved_by,
        now=now,
    )

    result = conn.execute(text("""
        UPDATE memory_suggestions
        SET status = 'approved',
            approved_by = :approved_by,
            approved_at = :now,
            updated_at = :now,
            edited_content = :edited_content,
            approved_fact_id = :approved_fact_id
        WHERE project_id = :project_id
          AND id = :suggestion_id
          AND status = 'pending'
    """), {
        "project_id": project_id,
        "suggestion_id": suggestion_id,
        "approved_by": approved_by,
        "now": now,
        "edited_content": edited,
        "approved_fact_id": fact["id"],
    })
    if result.rowcount == 0:
        raise ValueError("bootstrap.py: suggestion is not pending")

    approved_suggestion = _get_suggestion_row(conn, project_id, suggestion_id)
    return {
        "suggestion": approved_suggestion,
        "fact": fact,
    }


def _approval_response(result: dict) -> dict:
    return {
        "suggestion": _sanitize_suggestion(result.get("suggestion") or {}),
        "fact": _sanitize_fact(result.get("fact") or {}),
    }


def approve_suggestion(
    project_id: str,
    suggestion_id: str,
    approved_by: str = "api",
    edited_content: str | None = None,
) -> dict:
    """
    Approve a pending suggestion into an active memory fact, atomically.

    Validation, fact insertion, and the suggestion status update all happen in
    one transaction: if any step fails (blocked content, duplicate, write
    error) nothing is committed and the suggestion stays pending.

    If edited_content is provided, the edited text is validated through the
    same memory gate, inserted as the fact, and recorded in
    memory_suggestions.edited_content. The original suggestion content is never
    overwritten.
    """
    now = _utc_now()

    try:
        with engine.begin() as conn:
            result = _approve_suggestion_in_conn(
                conn,
                project_id=project_id,
                suggestion_id=suggestion_id,
                approved_by=approved_by,
                edited_content=edited_content,
                now=now,
            )
    except ValueError:
        raise
    except IntegrityError as error:
        raise ValueError(MEMORY_DUPLICATE_ERROR) from error
    except Exception as error:
        raise RuntimeError(f"bootstrap.py: approve_suggestion failed: {error}") from error

    return _approval_response(result)


def approve_suggestion_and_supersede(
    project_id: str,
    suggestion_id: str,
    old_fact_id: str,
    reason: str,
    approved_by: str = "api",
    edited_content: str | None = None,
) -> dict:
    """
    Approve a pending suggestion and mark an old active fact historical.

    This composes the existing approval validation/insert path with the M3D2
    supersession helper in a single transaction.
    """
    now = _utc_now()
    try:
        with engine.begin() as conn:
            approval = _approve_suggestion_in_conn(
                conn,
                project_id=project_id,
                suggestion_id=suggestion_id,
                approved_by=approved_by,
                edited_content=edited_content,
                now=now,
            )
            superseded_fact = supersede_fact_in_conn(
                conn,
                project_id=project_id,
                old_fact_id=old_fact_id,
                new_fact_id=approval["fact"]["id"],
                reason=reason,
                now=now,
            )
    except ValueError:
        raise
    except IntegrityError as error:
        raise ValueError(MEMORY_DUPLICATE_ERROR) from error
    except Exception as error:
        raise RuntimeError(
            f"bootstrap.py: approve_suggestion_and_supersede failed: {error}"
        ) from error

    return {
        "suggestion": _sanitize_suggestion(approval.get("suggestion") or {}),
        "fact": _sanitize_fact(approval.get("fact") or {}),
        "superseded_fact": _sanitize_fact(superseded_fact),
    }


def reject_suggestion(
    project_id: str,
    suggestion_id: str,
    reason: str,
    rejected_by: str = "api",
) -> dict:
    reason_value = (reason or "").strip()
    if len(reason_value) < 4:
        raise ValueError("bootstrap.py: rejection reason is required")
    suggestion = _get_suggestion(project_id, suggestion_id)
    if suggestion is None:
        raise ValueError("bootstrap.py: suggestion not found")
    if suggestion["status"] != "pending":
        raise ValueError("bootstrap.py: suggestion is not pending")

    now = _utc_now()
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE memory_suggestions
            SET status = 'rejected',
                rejected_by = :rejected_by,
                rejected_at = :now,
                rejection_reason = :reason,
                updated_at = :now
            WHERE project_id = :project_id
              AND id = :suggestion_id
        """), {
            "project_id": project_id,
            "suggestion_id": suggestion_id,
            "rejected_by": rejected_by,
            "reason": reason_value,
            "now": now,
        })
        conn.commit()

    return _sanitize_suggestion(_get_suggestion(project_id, suggestion_id) or {})
