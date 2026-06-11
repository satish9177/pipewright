"""
test_memory_pre_m1_migration.py
ARCH-M1 regression tests: the pre-M1 unscoped-row archive sweep is a one-time
startup migration, not hot-path work.

Before ARCH-M1 the sweep ran on every memory read/write (add_fact,
load_hard_facts, list_all_facts), taking a SQLite write lock for a no-op table
scan once the deployment was migrated. These tests pin the new contract:
  - normal reads/writes do NOT run the sweep,
  - the migration still archives legacy unscoped rows,
  - the migration runs at most once per process.

No API calls. No Gemini.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.memory import memory_store
from backend.memory.memory_store import (
    PRE_M1_ARCHIVE_REASON,
    add_fact,
    list_all_facts,
    load_hard_facts,
    migrate_unscoped_pre_m1_memory,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def memory_project_id():
    project_id = f"proj-{uuid.uuid4()}"
    yield project_id
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM memory_facts WHERE project_id = :p"),
            {"p": project_id},
        )


def _insert_legacy_unscoped_row() -> str:
    legacy_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO memory_facts
            (id, content, source, added_by, status, is_stale)
            VALUES (:id, 'legacy unscoped row', 'legacy', 'test', 'active', 0)
        """), {"id": legacy_id})
    return legacy_id


def test_reads_and_writes_do_not_run_archive_sweep(memory_project_id, monkeypatch):
    calls = {"count": 0}

    def _spy() -> None:
        calls["count"] += 1

    monkeypatch.setattr(memory_store, "_archive_unscoped_pre_m1_memory", _spy)

    add_fact(memory_project_id, "Backend uses FastAPI", category="stack")
    load_hard_facts(memory_project_id)
    load_hard_facts(None)
    list_all_facts(memory_project_id)
    list_all_facts()

    # ARCH-M1: none of the hot-path operations sweep anymore.
    assert calls["count"] == 0


def test_migration_archives_legacy_unscoped_rows(monkeypatch):
    legacy_id = _insert_legacy_unscoped_row()
    try:
        # Force a run regardless of process-once guard state (other tests or app
        # startup may have already migrated this process). monkeypatch restores
        # the guard afterward.
        monkeypatch.setattr(memory_store, "_pre_m1_migration_done", False)
        migrate_unscoped_pre_m1_memory()

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT status, is_stale, archived_reason "
                "FROM memory_facts WHERE id = :id"
            ), {"id": legacy_id}).fetchone()
        assert row[0] == "archived"
        assert row[1] == 1
        assert row[2] == PRE_M1_ARCHIVE_REASON
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM memory_facts WHERE id = :id"),
                {"id": legacy_id},
            )


def test_migration_runs_at_most_once_per_process(monkeypatch):
    calls = {"count": 0}

    def _spy() -> None:
        calls["count"] += 1

    monkeypatch.setattr(memory_store, "_archive_unscoped_pre_m1_memory", _spy)
    monkeypatch.setattr(memory_store, "_pre_m1_migration_done", False)

    migrate_unscoped_pre_m1_memory()
    migrate_unscoped_pre_m1_memory()
    migrate_unscoped_pre_m1_memory()

    # Guard collapses repeated startup calls to a single DB sweep.
    assert calls["count"] == 1
