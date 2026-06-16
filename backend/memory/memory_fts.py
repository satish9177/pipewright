"""
Explicit lifecycle for the dormant project-memory FTS5 scaffold.

The FTS table is derived/rebuildable infrastructure only. It is not a source of
truth and is not read by prompt construction or runtime memory selection.
"""

from __future__ import annotations

from sqlalchemy import text

from backend.db.database import (
    MEMORY_FTS_TABLE,
    _ensure_memory_fts_shape,
    _sqlite_fts5_available,
    engine,
)
from backend.memory.memory_store import _validate_project_id


def _memory_fts_table_exists(conn) -> bool:
    row = conn.execute(text("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = :table_name
    """), {"table_name": MEMORY_FTS_TABLE}).fetchone()
    return row is not None


def rebuild_memory_fts(project_id: str) -> int:
    """
    Rebuild one project's derived FTS index rows from canonical memory_facts.

    Only active, project-scoped, non-stale facts are copied. Suggestions,
    inactive lifecycle states, evidence/rationale fields, run/chunk content,
    repo file_index content, and vectors are never read here.
    """
    project_id = _validate_project_id(project_id)

    with engine.begin() as conn:
        if not _sqlite_fts5_available(conn):
            return 0

        _ensure_memory_fts_shape(conn)
        conn.execute(
            text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        conn.execute(text(f"""
            INSERT INTO {MEMORY_FTS_TABLE} (
                content,
                fact_id,
                project_id,
                category,
                scope,
                content_hash
            )
            SELECT
                content,
                id,
                project_id,
                category,
                scope,
                content_hash
            FROM memory_facts
            WHERE project_id = :project_id
              AND project_id IS NOT NULL
              AND TRIM(project_id) != ''
              AND is_stale = 0
              AND status = 'active'
            ORDER BY id
        """), {"project_id": project_id})
        row = conn.execute(text(f"""
            SELECT COUNT(*) FROM {MEMORY_FTS_TABLE}
            WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchone()
        return int(row[0]) if row is not None else 0


def _match_memory_fts_for_project(
    project_id: str,
    query: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """
    Internal scoped MATCH helper for scaffold tests only.

    This is deliberately private and project-scoped. It is not a product
    retrieval/ranking API.
    """
    project_id = _validate_project_id(project_id)
    query = (query or "").strip()
    limit = max(0, int(limit))
    if not query or limit == 0:
        return []

    with engine.connect() as conn:
        if not _sqlite_fts5_available(conn) or not _memory_fts_table_exists(conn):
            return []
        rows = conn.execute(text(f"""
            SELECT fact_id, project_id, category, scope, content_hash, content
            FROM {MEMORY_FTS_TABLE}
            WHERE {MEMORY_FTS_TABLE} MATCH :query
              AND project_id = :project_id
            ORDER BY fact_id
            LIMIT :limit
        """), {
            "project_id": project_id,
            "query": query,
            "limit": limit,
        }).fetchall()
    return [dict(row._mapping) for row in rows]
