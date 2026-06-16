"""
Tests for Row 19 PR-A's dormant memory FTS5 scaffold.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from backend.db import database
from backend.db.database import MEMORY_FTS_TABLE, engine, init_db
from backend.memory import prompt_builder
from backend.memory.memory_fts import (
    _match_memory_fts_for_project,
    rebuild_memory_fts,
)
from backend.memory.memory_store import (
    add_fact,
    archive_fact,
    compute_content_hash,
    mark_fact_stale,
    supersede_fact,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def memory_project_ids():
    project_ids = []
    yield project_ids
    with engine.begin() as conn:
        for project_id in project_ids:
            if _table_exists(conn, MEMORY_FTS_TABLE):
                conn.execute(
                    text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE project_id = :p"),
                    {"p": project_id},
                )
            conn.execute(
                text("DELETE FROM memory_suggestions WHERE project_id = :p"),
                {"p": project_id},
            )
            conn.execute(
                text("DELETE FROM memory_facts WHERE project_id = :p"),
                {"p": project_id},
            )


def _make_project_id(memory_project_ids, label="project") -> str:
    project_id = f"{label}-{uuid.uuid4().hex}"
    memory_project_ids.append(project_id)
    return project_id


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(text("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = :table_name
    """), {"table_name": table_name}).fetchone()
    return row is not None


def _drop_memory_fts_table() -> None:
    with engine.begin() as conn:
        if _table_exists(conn, MEMORY_FTS_TABLE):
            conn.execute(text(f"DROP TABLE {MEMORY_FTS_TABLE}"))


def _fts_available() -> bool:
    with engine.connect() as conn:
        return database._sqlite_fts5_available(conn)


def _require_fts5() -> None:
    if not _fts_available():
        pytest.skip("SQLite FTS5 is not available in this environment")


def _direct_fts5_probe(conn) -> bool:
    try:
        conn.execute(text("SAVEPOINT pipewright_direct_fts5_probe"))
        conn.execute(text("""
            CREATE VIRTUAL TABLE temp.pipewright_direct_fts5_probe
            USING fts5(content)
        """))
        conn.execute(text("DROP TABLE temp.pipewright_direct_fts5_probe"))
        conn.execute(text("RELEASE SAVEPOINT pipewright_direct_fts5_probe"))
        return True
    except Exception:
        try:
            conn.execute(text("ROLLBACK TO SAVEPOINT pipewright_direct_fts5_probe"))
        except Exception:
            pass
        try:
            conn.execute(text("RELEASE SAVEPOINT pipewright_direct_fts5_probe"))
        except Exception:
            pass
        return False


def _fts_rows(project_id: str) -> list[dict]:
    with engine.connect() as conn:
        if not _table_exists(conn, MEMORY_FTS_TABLE):
            return []
        rows = conn.execute(text(f"""
            SELECT fact_id, project_id, category, scope, content_hash, content
            FROM {MEMORY_FTS_TABLE}
            WHERE project_id = :project_id
            ORDER BY fact_id
        """), {"project_id": project_id}).fetchall()
    return [dict(row._mapping) for row in rows]


def _insert_pending_suggestion(project_id: str, content: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO memory_suggestions (
                id,
                project_id,
                content,
                category,
                scope,
                priority,
                source,
                evidence_path,
                evidence_excerpt,
                status,
                content_hash,
                rationale
            )
            VALUES (
                :id,
                :project_id,
                :content,
                'stack',
                'backend',
                100,
                'bootstrap',
                'backend/example.py',
                'suggestion evidence must not be indexed',
                'pending',
                :content_hash,
                'suggestion rationale must not be indexed'
            )
        """), {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "content": content,
            "content_hash": compute_content_hash(content),
        })


def test_fts5_capability_probe_matches_environment():
    with engine.connect() as conn:
        direct_available = _direct_fts5_probe(conn)

    with engine.connect() as conn:
        assert database._sqlite_fts5_available(conn) is direct_available
        if direct_available:
            assert database._sqlite_fts5_available(conn) is True


def test_simulated_fts5_absent_init_and_ensure_noop(monkeypatch):
    _drop_memory_fts_table()
    monkeypatch.setattr(database, "_sqlite_fts5_available", lambda conn: False)

    init_db()
    with engine.begin() as conn:
        database._ensure_memory_fts_shape(conn)
        assert not _table_exists(conn, MEMORY_FTS_TABLE)


def test_ensure_memory_fts_shape_is_idempotent():
    _require_fts5()

    with engine.begin() as conn:
        database._ensure_memory_fts_shape(conn)
        database._ensure_memory_fts_shape(conn)
        assert _table_exists(conn, MEMORY_FTS_TABLE)
        rows = conn.execute(text("""
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
        """), {"table_name": MEMORY_FTS_TABLE}).fetchone()
        table_sql = conn.execute(text("""
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
        """), {"table_name": MEMORY_FTS_TABLE}).scalar_one()

    assert rows[0] == 1
    for metadata_column in ("fact_id", "project_id", "category", "scope", "content_hash"):
        assert f"{metadata_column} UNINDEXED" in table_sql


def test_rebuild_populates_only_active_scoped_non_stale_facts(memory_project_ids):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids, "project-a")
    other_project_id = _make_project_id(memory_project_ids, "project-b")
    active = add_fact(
        project_id,
        "Alpha active memory fact for backend search",
        category="stack",
        scope="backend",
    )
    archived = add_fact(project_id, "Alpha archived memory must not index")
    stale = add_fact(project_id, "Alpha stale memory must not index")
    historical = add_fact(project_id, "Alpha historical memory must not index")
    add_fact(other_project_id, "Alpha cross project memory must not index")
    _insert_pending_suggestion(
        project_id,
        "Alpha pending suggestion content must not index",
    )
    legacy_id = str(uuid.uuid4())

    try:
        archive_fact(project_id, archived["id"], "No longer true")
        mark_fact_stale(project_id, stale["id"], "No longer current")
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE memory_facts
                SET status = 'historical', is_stale = 1
                WHERE id = :id
            """), {"id": historical["id"]})
            conn.execute(text("""
                INSERT INTO memory_facts (
                    id,
                    content,
                    category,
                    scope,
                    priority,
                    source,
                    added_by,
                    status,
                    is_stale,
                    content_hash
                )
                VALUES (
                    :id,
                    'Alpha unscoped legacy memory must not index',
                    'stack',
                    'global',
                    100,
                    'legacy',
                    'test',
                    'active',
                    0,
                    :content_hash
                )
            """), {
                "id": legacy_id,
                "content_hash": compute_content_hash(
                    "Alpha unscoped legacy memory must not index"
                ),
            })

        assert rebuild_memory_fts(project_id) == 1

        rows = _fts_rows(project_id)
        assert [row["fact_id"] for row in rows] == [active["id"]]
        assert rows[0]["content"] == active["content"]
        assert rows[0]["project_id"] == project_id
        assert rows[0]["category"] == "stack"
        assert rows[0]["scope"] == "backend"
        assert rows[0]["content_hash"] == active["content_hash"]
        indexed_text = "\n".join(row["content"] for row in rows)
        for excluded in (
            "archived memory",
            "stale memory",
            "historical memory",
            "cross project memory",
            "pending suggestion",
            "unscoped legacy memory",
            "suggestion evidence",
            "suggestion rationale",
        ):
            assert excluded not in indexed_text
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM memory_facts WHERE id = :id"),
                {"id": legacy_id},
            )


def test_rebuild_is_deterministic_and_idempotent(memory_project_ids):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Bravo memory alpha fact", category="stack")
    add_fact(project_id, "Bravo memory beta fact", category="test")

    first_count = rebuild_memory_fts(project_id)
    first_rows = _fts_rows(project_id)
    second_count = rebuild_memory_fts(project_id)
    second_rows = _fts_rows(project_id)

    assert first_count == 2
    assert second_count == 2
    assert second_rows == first_rows


def test_archive_and_supersede_disappear_after_rebuild(memory_project_ids):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    archived = add_fact(project_id, "Charlie archived fact initially indexed")
    old = add_fact(project_id, "Charlie old framework fact")
    new = add_fact(project_id, "Charlie new framework fact")

    rebuild_memory_fts(project_id)
    before_contents = {row["content"] for row in _fts_rows(project_id)}
    assert archived["content"] in before_contents
    assert old["content"] in before_contents
    assert new["content"] in before_contents

    archive_fact(project_id, archived["id"], "No longer true")
    supersede_fact(
        project_id,
        old["id"],
        new["id"],
        "New framework fact replaces the old one.",
    )
    rebuild_memory_fts(project_id)

    after_contents = {row["content"] for row in _fts_rows(project_id)}
    assert archived["content"] not in after_contents
    assert old["content"] not in after_contents
    assert new["content"] in after_contents


def test_internal_match_helper_is_project_scoped(memory_project_ids):
    _require_fts5()
    project_a = _make_project_id(memory_project_ids, "project-a")
    project_b = _make_project_id(memory_project_ids, "project-b")
    fact_a = add_fact(project_a, "Delta sharedtoken memory for project A")
    fact_b = add_fact(project_b, "Delta sharedtoken memory for project B")
    rebuild_memory_fts(project_a)
    rebuild_memory_fts(project_b)

    matches = _match_memory_fts_for_project(project_a, "sharedtoken")

    assert {match["fact_id"] for match in matches} == {fact_a["id"]}
    assert fact_b["id"] not in {match["fact_id"] for match in matches}
    assert {match["project_id"] for match in matches} == {project_a}


def test_init_db_succeeds_on_fresh_and_existing_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh_pipewright.db"
    fresh_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    monkeypatch.setattr(database, "engine", fresh_engine)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    try:
        database.init_db()
        database.init_db()
        with fresh_engine.connect() as conn:
            assert _table_exists(conn, "memory_facts")
    finally:
        fresh_engine.dispose()


def test_prompt_builder_output_is_identical_with_and_without_fts(
    monkeypatch,
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Echo backend memory remains advisory", category="stack")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)
    _drop_memory_fts_table()

    without_fts = prompt_builder.build_project_memory_block(
        project_id,
        role="planner",
    )
    with engine.begin() as conn:
        database._ensure_memory_fts_shape(conn)
    rebuild_memory_fts(project_id)
    with_fts = prompt_builder.build_project_memory_block(
        project_id,
        role="planner",
    )

    assert without_fts == with_fts
