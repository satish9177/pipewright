"""
test_foundation.py
Tests for Day 1 foundation:
  - database initialization
  - memory store operations
  - checkpoint store operations
"""

import uuid
import pytest
from backend.memory.memory_store import (
    add_fact,
    load_hard_facts,
    flag_stale_memories,
    list_all_facts
)
from backend.checkpoint.checkpoint_store import (
    save_checkpoint,
    load_last_checkpoint,
    load_step_checkpoint
)


def test_add_and_load_facts():
    add_fact("Tech stack: Python FastAPI", "test", "founder")
    add_fact("Database: SQLite via SQLAlchemy", "test", "founder")
    facts = load_hard_facts()
    assert "FastAPI" in facts
    assert "SQLite" in facts


def test_empty_content_raises():
    with pytest.raises(ValueError):
        add_fact("", "test", "founder")


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
        step="plan",
        output={"goal": "test goal"},
        handoff_contract={"handoff_from": "planner"},
        git_hash="abc123",
        tests_passed=True
    )
    assert cp["step"] == "plan"

    loaded = load_last_checkpoint(run_id)
    assert loaded is not None
    assert loaded["step"] == "plan"
    assert loaded["git_commit_hash"] == "abc123"


def test_checkpoint_fails_without_tests_passed():
    run_id = str(uuid.uuid4())
    with pytest.raises(ValueError):
        save_checkpoint(
            run_id=run_id,
            step="code",
            output={},
            handoff_contract={},
            git_hash="abc123",
            tests_passed=False
        )


def test_load_step_checkpoint():
    run_id = str(uuid.uuid4())
    save_checkpoint(
        run_id=run_id,
        step="plan",
        output={"goal": "step test"},
        handoff_contract={"handoff_from": "planner"},
        git_hash="def456",
        tests_passed=True
    )
    loaded = load_step_checkpoint(run_id, "plan")
    assert loaded is not None
    assert loaded["step"] == "plan"


def test_missing_checkpoint_returns_none():
    result = load_last_checkpoint("nonexistent-run-id")
    assert result is None
