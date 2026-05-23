"""
test_orchestrator.py
Tests for orchestrator.py
Unit tests only - mock all pipeline stages.
Do not make real AI calls here.
The real end-to-end test is done manually.
"""

import pytest
from unittest.mock import AsyncMock, patch
from backend.pipeline.orchestrator import run_pipeline

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_pipeline_fails_gracefully_on_planner_error():
    with patch(
        "backend.pipeline.orchestrator.run_planner",
        new_callable=AsyncMock,
        side_effect=RuntimeError("planner failed")
    ):
        result = await run_pipeline("Add a health endpoint")

    assert result["status"] == "failed"
    assert result["failed_stage"] == "plan"
    assert "planner failed" in result["error"]


@pytest.mark.asyncio
async def test_pipeline_creates_run_record():
    from backend.db.database import engine
    from sqlalchemy import text

    with patch(
        "backend.pipeline.orchestrator.run_planner",
        new_callable=AsyncMock,
        side_effect=RuntimeError("stop early")
    ):
        result = await run_pipeline("Test run creation")

    run_id = result["run_id"]
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT * FROM pipeline_runs WHERE id = :id"
        ), {"id": run_id}).fetchone()

    assert row is not None
    assert row._mapping["status"] == "failed"


@pytest.mark.asyncio
async def test_pipeline_fails_on_test_failure(monkeypatch):
    from backend.models.handoff import (
        PlannerHandoff, CoderHandoff,
        PatchResult, PipelineTestResult
    )

    mock_plan = PlannerHandoff(
        run_id="x", feature_description="test",
        goal="test goal", steps=["step1", "step2"],
        files_to_create=[], files_to_modify=[],
        files_to_read=[], out_of_scope=[], risks=[],
        suggested_memory_entries=[]
    )
    mock_coder = CoderHandoff(
        run_id="x", feature_description="test",
        files_changed=[],
        summary="test summary"
    )
    mock_patch = PatchResult(
        run_id="x", success=True, diff="",
        pre_patch_git_hash="abc",
        post_patch_git_hash="def",
        files_applied=[]
    )
    mock_test = PipelineTestResult(
        run_id="x", passed=False,
        output="2 failed", failed_tests=2
    )

    with patch(
        "backend.pipeline.orchestrator.run_planner",
        new_callable=AsyncMock,
        return_value=mock_plan
    ), patch(
        "backend.pipeline.orchestrator.run_coder",
        new_callable=AsyncMock,
        return_value=mock_coder
    ), patch(
        "backend.pipeline.orchestrator.apply_patch",
        return_value=mock_patch
    ), patch(
        "backend.pipeline.orchestrator.run_tests",
        return_value=mock_test
    ):
        result = await run_pipeline("Feature that fails tests")

    assert result["status"] == "failed"
    assert result["failed_stage"] == "test"
