"""
test_planner.py
Tests for planner.py pipeline stage.
Requires GEMINI_API_KEY in .env to run.
These tests make real API calls.
"""
import uuid
import pytest
from types import SimpleNamespace
from backend.memory.memory_store import add_fact
from backend.pipeline.planner import run_planner
from backend.pipeline import planner

pytestmark = pytest.mark.api


def _planner_response(run_id: str):
    text = (
        "{"
        '"handoff_from": "planner",'
        '"handoff_to": "coder",'
        f'"run_id": "{run_id}",'
        '"feature_description": "Add ping endpoint",'
        '"goal": "Create a ping endpoint.",'
        '"steps": ["Add route", "Return response"],'
        '"files_to_create": [],'
        '"files_to_modify": ["backend/main.py"],'
        '"files_to_read": [],'
        '"out_of_scope": [],'
        '"risks": [],'
        '"suggested_memory_entries": []'
        "}"
    )
    usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=20)
    return SimpleNamespace(text=text, usage_metadata=usage)


class _PlannerModel:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_content(self, prompt):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _patch_planner_dependencies(monkeypatch, model):
    monkeypatch.setattr(planner, "load_hard_facts", lambda: "")
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(planner.genai, "configure", lambda api_key: None)
    monkeypatch.setattr(
        planner.genai,
        "GenerationConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        planner.genai,
        "GenerativeModel",
        lambda **kwargs: model,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_429_retry_uses_asyncio_sleep(monkeypatch):
    run_id = str(uuid.uuid4())
    model = _PlannerModel([
        RuntimeError("429 rate limit"),
        _planner_response(run_id),
    ])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner.asyncio, "sleep", fake_sleep)

    result = await run_planner("Add ping endpoint", run_id)

    assert result.run_id == run_id
    assert sleeps == [60]
    assert not hasattr(planner, "time")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_429_retry_failure_raises_runtime_error(monkeypatch):
    run_id = str(uuid.uuid4())
    model = _PlannerModel([
        RuntimeError("429 rate limit"),
        RuntimeError("still rate limited"),
    ])

    async def fake_sleep(seconds):
        return None

    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError) as error:
        await run_planner("Add ping endpoint", run_id)

    assert "planner.py: Failed after rate limit retry" in str(error.value)

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
