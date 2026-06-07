"""
test_foundation.py
Tests for Day 1 foundation:
  - database initialization
  - memory store operations
  - checkpoint store operations
"""

import uuid
import pytest
from sqlalchemy import create_engine, text
from backend.db import database
from backend.memory.memory_store import (
    add_fact,
    load_hard_facts,
    flag_stale_memories,
    list_all_facts
)
from backend.checkpoint.checkpoint_store import (
    save_checkpoint,
    load_last_checkpoint,
    load_step_checkpoint,
    load_chunk_step_checkpoint
)

pytestmark = pytest.mark.unit

def test_add_and_load_facts():
    project_id = f"test-project-{uuid.uuid4()}"
    add_fact(project_id, "Tech stack: Python FastAPI", source="test", added_by="founder")
    add_fact(
        project_id,
        "Database: SQLite via SQLAlchemy",
        source="test",
        added_by="founder",
    )
    facts = load_hard_facts(project_id)
    assert "FastAPI" in facts
    assert "SQLite" in facts


def test_empty_content_raises():
    with pytest.raises(ValueError):
        add_fact(f"test-project-{uuid.uuid4()}", "", source="test", added_by="founder")


def test_flag_stale_memories():
    count = flag_stale_memories(days=90)
    assert isinstance(count, int)


def test_list_all_facts_returns_list():
    result = list_all_facts()
    assert isinstance(result, list)


def test_save_and_load_checkpoint():
    run_id = str(uuid.uuid4())
    cp = save_checkpoint(
        run_id=run_id,
        step="test",
        output={"goal": "test goal"},
        handoff_contract={"handoff_from": "planner"},
        git_hash="abc123",
        tests_passed=True,
        step_completed=True,
    )
    assert cp["step"] == "test"

    loaded = load_last_checkpoint(run_id)
    assert loaded is not None
    assert loaded["step"] == "test"
    assert loaded["git_commit_hash"] == "abc123"
    assert loaded["chunk_number"] == 0


def test_checkpoint_fails_without_step_completed():
    run_id = str(uuid.uuid4())
    with pytest.raises(ValueError):
        save_checkpoint(
            run_id=run_id,
            step="code",
            output={},
            handoff_contract={},
            git_hash="abc123",
            tests_passed=False,
            step_completed=False,
        )


def test_non_test_checkpoint_does_not_set_tests_passed():
    run_id = str(uuid.uuid4())
    cp = save_checkpoint(
        run_id=run_id,
        step="plan",
        output={"goal": "step test"},
        handoff_contract={"handoff_from": "planner"},
        git_hash="def456",
        tests_passed=False,
        step_completed=True,
    )

    assert cp["tests_passed"] is False
    loaded = load_step_checkpoint(run_id, "plan")
    assert loaded is not None
    assert loaded["tests_passed"] == 0


def test_load_step_checkpoint():
    run_id = str(uuid.uuid4())
    save_checkpoint(
        run_id=run_id,
        step="plan",
        output={"goal": "step test"},
        handoff_contract={"handoff_from": "planner"},
        git_hash="def456",
        tests_passed=False,
        step_completed=True,
    )
    loaded = load_step_checkpoint(run_id, "plan")
    assert loaded is not None
    assert loaded["step"] == "plan"


def test_missing_checkpoint_returns_none():
    result = load_last_checkpoint("nonexistent-run-id")
    assert result is None


def test_checkpoint_chunk_number_persistence():
    run_id = str(uuid.uuid4())
    cp = save_checkpoint(
        run_id=run_id,
        step="test",
        output={"result": "passed"},
        handoff_contract={"handoff_from": "tester"},
        git_hash="chunk123",
        tests_passed=True,
        step_completed=True,
        chunk_number=2
    )
    assert cp["chunk_number"] == 2

    loaded = load_chunk_step_checkpoint(run_id, 2, "test")
    assert loaded is not None
    assert loaded["chunk_number"] == 2
    assert loaded["step"] == "test"
    assert loaded["output"]["result"] == "passed"

    wrong_chunk = load_chunk_step_checkpoint(run_id, 1, "test")
    assert wrong_chunk is None


def test_legacy_chunk_checkpoint_still_loads():
    run_id = str(uuid.uuid4())
    save_checkpoint(
        run_id=run_id,
        step="plan",
        output={"goal": "legacy"},
        handoff_contract={"handoff_from": "planner"},
        git_hash="legacy123",
        tests_passed=False,
        step_completed=True,
    )

    loaded = load_chunk_step_checkpoint(run_id, 0, "plan")
    assert loaded is not None
    assert loaded["chunk_number"] == 0
    assert loaded["output"]["goal"] == "legacy"


def test_execute_schema_script_handles_semicolon_in_comment():
    # Regression for PR #15B: the old split(";") schema loader treated text
    # after a ';' inside a SQL comment as a statement, raising
    # "OperationalError near ...". executescript must handle this safely.
    table = f"pr15b_schema_{uuid.uuid4().hex}"
    schema = f"""
    -- this comment contains a semicolon; the loader must not choke on it
    CREATE TABLE IF NOT EXISTS {table} (
        id TEXT PRIMARY KEY,
        note TEXT DEFAULT 'a;b'  -- string literal with a semicolon too
    );
    """
    try:
        # Must not raise.
        database._execute_schema_script(schema)
        with database.engine.connect() as conn:
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = :name"
            ), {"name": table}).fetchone()
        assert exists is not None
    finally:
        with database.engine.connect() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            conn.commit()


def test_init_migration_adds_approval_gate_created_at():
    temp_engine = create_engine("sqlite:///:memory:")
    with temp_engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE pipeline_runs (
                id TEXT PRIMARY KEY,
                feature_description TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE projects (
                id TEXT PRIMARY KEY
            )
        """))
        conn.execute(text("""
            CREATE TABLE checkpoints (
                id TEXT PRIMARY KEY
            )
        """))
        conn.execute(text("""
            CREATE TABLE approval_gates (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                diff TEXT,
                test_results TEXT,
                ai_summary TEXT,
                plain_english_summary TEXT,
                risk_level TEXT DEFAULT 'medium',
                rejection_reason TEXT,
                decided_at DATETIME
            )
        """))
        conn.execute(text("""
            INSERT INTO approval_gates
            (id, run_id, step, status)
            VALUES ('gate-1', 'run-1', 'approval', 'pending')
        """))
        conn.commit()

        database._migrate_db(conn)
        conn.commit()

        columns = conn.execute(text(
            "PRAGMA table_info(approval_gates)"
        )).fetchall()
        column_names = {row._mapping["name"] for row in columns}
        created_at = conn.execute(text("""
            SELECT created_at FROM approval_gates
            WHERE id = 'gate-1'
        """)).fetchone()[0]
        gate_defaults = conn.execute(text("""
            SELECT chunk_number, approval_type FROM approval_gates
            WHERE id = 'gate-1'
        """)).fetchone()

    assert "created_at" in column_names
    assert "chunk_number" in column_names
    assert "approval_type" in column_names
    assert created_at is not None
    assert gate_defaults[0] == 0
    assert gate_defaults[1] == "legacy"


def test_init_migration_adds_nullable_run_start_context_columns():
    temp_engine = create_engine("sqlite:///:memory:")
    with temp_engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE pipeline_runs (
                id TEXT PRIMARY KEY,
                feature_description TEXT NOT NULL
            )
        """))
        conn.commit()

        database._migrate_db(conn)
        database._migrate_db(conn)
        conn.execute(text("""
            INSERT INTO pipeline_runs (id, feature_description)
            VALUES ('run-1', 'legacy run')
        """))
        conn.commit()

        columns = conn.execute(text(
            "PRAGMA table_info(pipeline_runs)"
        )).fetchall()
        column_names = {row._mapping["name"] for row in columns}
        row = conn.execute(text("""
            SELECT start_branch, start_head_sha
            FROM pipeline_runs
            WHERE id = 'run-1'
        """)).fetchone()

    assert "start_branch" in column_names
    assert "start_head_sha" in column_names
    assert row[0] is None
    assert row[1] is None
