"""
test_planner.py
Tests for planner.py pipeline stage.
Requires GEMINI_API_KEY in .env to run.
These tests make real API calls.
"""

import uuid
import pytest
from backend.memory.memory_store import add_fact
from backend.pipeline.planner import run_planner


@pytest.mark.asyncio
async def test_planner_returns_valid_handoff():
    add_fact(
        "Tech stack: Python 3.11 FastAPI backend",
        "test", "founder"
    )
    add_fact(
        "Database: SQLite via SQLAlchemy synchronous",
        "test", "founder"
    )
    add_fact(
        "All IDs are UUIDs not integers",
        "test", "founder"
    )

    run_id = str(uuid.uuid4())
    feature = (
        "Add a GET /runs/{run_id}/status endpoint "
        "that returns the current pipeline run status, "
        "current step, and created_at timestamp "
        "from the pipeline_runs table."
    )

    result = await run_planner(
        feature_description=feature,
        run_id=run_id
    )

    assert result.handoff_from == "planner"
    assert result.handoff_to == "coder"
    assert result.run_id == run_id
    assert len(result.steps) >= 2
    assert isinstance(result.files_to_create, list)
    assert isinstance(result.files_to_modify, list)
    assert result.goal and len(result.goal) > 10


@pytest.mark.asyncio
async def test_planner_works_with_no_memory():
    run_id = str(uuid.uuid4())
    feature = "Add a simple ping endpoint that returns pong"

    result = await run_planner(
        feature_description=feature,
        run_id=run_id
    )

    assert result.handoff_from == "planner"
    assert len(result.steps) >= 2
