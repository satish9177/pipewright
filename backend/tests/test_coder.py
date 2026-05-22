"""
test_coder.py
Tests for coder.py pipeline stage.
Requires GEMINI_API_KEY in .env to run.
These tests make real API calls.
The target repo is ai-workflow-platform.
Coder reads files from there but never writes.
"""

import uuid
import pytest
from backend.memory.memory_store import add_fact
from backend.models.handoff import PlannerHandoff
from backend.pipeline.coder import run_coder


def make_test_plan(run_id: str) -> PlannerHandoff:
    return PlannerHandoff(
        run_id=run_id,
        feature_description=(
            "Add a GET /health endpoint that returns "
            "status ok and current timestamp"
        ),
        goal="Create a simple health check endpoint",
        steps=[
            "Create a new route for GET /health",
            "Return JSON with status and timestamp",
            "No database calls needed"
        ],
        files_to_create=["backend/routes/health.py"],
        files_to_modify=["backend/main.py"],
        files_to_read=["backend/main.py"],
        out_of_scope=["authentication", "database"],
        risks=["main.py may not exist in target repo"],
        suggested_memory_entries=[]
    )


@pytest.mark.asyncio
async def test_coder_returns_valid_handoff():
    add_fact(
        "Tech stack: Python FastAPI",
        "test", "founder"
    )

    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"
    assert result.handoff_to == "patch_applier"
    assert result.run_id == run_id
    assert len(result.files_changed) > 0
    assert result.summary and len(result.summary) > 10

    for fc in result.files_changed:
        assert fc.action in ["create", "modify", "delete"]
        assert fc.path and len(fc.path) > 0
        assert fc.reason and len(fc.reason) > 0
        if fc.action != "delete":
            assert fc.content is not None
            assert len(fc.content) > 0


@pytest.mark.asyncio
async def test_coder_handles_missing_files_gracefully():
    run_id = str(uuid.uuid4())

    plan = PlannerHandoff(
        run_id=run_id,
        feature_description="Add a ping endpoint",
        goal="Create a ping endpoint that returns pong",
        steps=["Create ping route", "Return pong response"],
        files_to_create=["backend/routes/ping.py"],
        files_to_modify=[],
        files_to_read=["backend/routes/nonexistent_file.py"],
        out_of_scope=[],
        risks=["file may not exist"],
        suggested_memory_entries=[]
    )

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"
    assert len(result.files_changed) > 0
