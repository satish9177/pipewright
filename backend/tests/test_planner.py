"""
test_planner.py
Tests for planner.py pipeline stage.
Mocked unit tests are fast and local.
API-marked tests make real Gemini calls.
"""
import uuid
import pytest
from types import SimpleNamespace
from backend.config.keys import settings
from backend.db.database import engine
from backend.memory.memory_store import add_fact, archive_fact
from backend.memory.prompt_builder import build_project_memory_block as real_build_memory_block
from backend.pipeline.planner import run_planner
from backend.pipeline import planner
from sqlalchemy import text


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
        self.prompts = []

    def generate_content(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _patch_planner_dependencies(monkeypatch, model):
    monkeypatch.setattr(planner, "build_project_memory_block", lambda **kwargs: "")
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


def _cleanup_memory(project_id: str):
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM memory_facts WHERE project_id = :project_id
        """), {"project_id": project_id})


def _skip_without_gemini_key():
    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY is required for live Gemini test")


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
    checkpoint_calls = []
    monkeypatch.setattr(
        planner,
        "save_checkpoint",
        lambda **kwargs: checkpoint_calls.append(kwargs),
    )
    monkeypatch.setattr(planner.asyncio, "sleep", fake_sleep)

    result = await run_planner("Add ping endpoint", run_id)

    assert result.run_id == run_id
    assert sleeps == [60]
    assert not hasattr(planner, "time")
    assert checkpoint_calls[0]["step"] == "plan"
    assert checkpoint_calls[0]["tests_passed"] is False
    assert checkpoint_calls[0]["step_completed"] is True


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_includes_project_memory(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-project-{uuid.uuid4().hex}"
    model = _PlannerModel([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner, "build_project_memory_block", real_build_memory_block)
    add_fact(project_id, "Backend uses FastAPI memory", category="stack", scope="backend")

    try:
        await run_planner("Add ping endpoint", run_id, project_id=project_id)
    finally:
        _cleanup_memory(project_id)

    prompt = model.prompts[0]
    assert "=== PROJECT MEMORY" in prompt
    assert "[stack/backend] Backend uses FastAPI memory" in prompt
    assert "source code and explicit user instructions win" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_injection_skips_empty_memory(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-empty-{uuid.uuid4().hex}"
    model = _PlannerModel([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner, "build_project_memory_block", real_build_memory_block)

    await run_planner("Add ping endpoint", run_id, project_id=project_id)

    assert "=== PROJECT MEMORY" not in model.prompts[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_injection_is_project_scoped(monkeypatch):
    run_id = str(uuid.uuid4())
    project_a = f"planner-a-{uuid.uuid4().hex}"
    project_b = f"planner-b-{uuid.uuid4().hex}"
    model = _PlannerModel([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner, "build_project_memory_block", real_build_memory_block)
    add_fact(project_a, "Project A planner memory", category="stack")
    add_fact(project_b, "Project B planner memory", category="stack")

    try:
        await run_planner("Add ping endpoint", run_id, project_id=project_a)
    finally:
        _cleanup_memory(project_a)
        _cleanup_memory(project_b)

    assert "Project A planner memory" in model.prompts[0]
    assert "Project B planner memory" not in model.prompts[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_injection_excludes_archived_stale_historical(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-status-{uuid.uuid4().hex}"
    model = _PlannerModel([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner, "build_project_memory_block", real_build_memory_block)
    active = add_fact(project_id, "Active planner memory", category="stack")
    archived = add_fact(project_id, "Archived planner memory", category="stack")
    stale = add_fact(project_id, "Stale planner memory", category="stack")
    historical = add_fact(project_id, "Historical planner memory", category="stack")
    archive_fact(project_id, archived["id"], "No longer true")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts SET status = 'stale', is_stale = 1 WHERE id = :id
        """), {"id": stale["id"]})
        conn.execute(text("""
            UPDATE memory_facts SET status = 'historical' WHERE id = :id
        """), {"id": historical["id"]})

    try:
        await run_planner("Add ping endpoint", run_id, project_id=project_id)
    finally:
        _cleanup_memory(project_id)

    prompt = model.prompts[0]
    assert "Active planner memory" in prompt
    assert "Archived planner memory" not in prompt
    assert "Stale planner memory" not in prompt
    assert "Historical planner memory" not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_still_works_without_project_id(monkeypatch):
    run_id = str(uuid.uuid4())
    model = _PlannerModel([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, model)
    monkeypatch.setattr(planner, "build_project_memory_block", real_build_memory_block)

    result = await run_planner("Add ping endpoint", run_id)

    assert result.run_id == run_id
    assert "=== PROJECT MEMORY" not in model.prompts[0]


@pytest.mark.api
@pytest.mark.asyncio
async def test_planner_returns_valid_handoff():
    _skip_without_gemini_key()
    project_id = str(uuid.uuid4())
    add_fact(
        project_id,
        "Tech stack: Python 3.11 FastAPI backend",
        source="test", added_by="founder"
    )
    add_fact(
        project_id,
        "Database: SQLite via SQLAlchemy synchronous",
        source="test", added_by="founder"
    )
    add_fact(
        project_id,
        "All IDs are UUIDs not integers",
        source="test", added_by="founder"
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


@pytest.mark.api
@pytest.mark.asyncio
async def test_planner_works_with_no_memory():
    _skip_without_gemini_key()
    run_id = str(uuid.uuid4())
    feature = "Add a simple ping endpoint that returns pong"

    result = await run_planner(
        feature_description=feature,
        run_id=run_id
    )

    assert result.handoff_from == "planner"
    assert len(result.steps) >= 2
