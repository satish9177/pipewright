"""
test_planner.py
Tests for planner.py pipeline stage.
Mocked unit tests are fast and local.
API-marked tests make real Gemini calls.
"""
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from backend.config.keys import settings
from backend.db.database import engine
from backend.llm.base import LLMResponse
from backend.llm.errors import LLMError
from backend.llm.role_config import Role
from backend.memory.memory_store import add_fact, archive_fact
from backend.memory.prompt_builder import (
    build_project_memory_block_detailed as real_build_memory_block_detailed,
)
from backend.pipeline.planner import run_planner
from backend.pipeline import planner
from sqlalchemy import text


def _empty_memory_result(**kwargs):
    """Stand-in MemoryBlockBuildResult (duck-typed) for memory-free unit tests."""
    return SimpleNamespace(
        block="",
        role=kwargs.get("role"),
        token_budget=0,
        category_policy=(),
        included_entries=(),
        excluded_entries=(),
    )


def _planner_response(run_id: str):
    return (
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


class _PlannerLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.roles = []

    async def complete(self, role, request):
        self.roles.append(role)
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LLMResponse(
            text=response,
            provider="fake",
            model=request.model,
            input_tokens=10,
            output_tokens=20,
            finish_reason="stop",
        )


def _patch_planner_dependencies(monkeypatch, llm):
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(planner, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(
        planner,
        "complete_for_role",
        llm.complete,
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
    llm = _PlannerLLM([
        RuntimeError("429 rate limit"),
        _planner_response(run_id),
    ])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _patch_planner_dependencies(monkeypatch, llm)
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
    assert llm.roles == [Role.PLANNER, Role.PLANNER]
    assert checkpoint_calls[0]["step"] == "plan"
    assert checkpoint_calls[0]["tests_passed"] is False
    assert checkpoint_calls[0]["step_completed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_429_retry_failure_raises_runtime_error(monkeypatch):
    run_id = str(uuid.uuid4())
    llm = _PlannerLLM([
        RuntimeError("429 rate limit"),
        RuntimeError("still rate limited"),
    ])

    async def fake_sleep(seconds):
        return None

    _patch_planner_dependencies(monkeypatch, llm)
    monkeypatch.setattr(planner.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError) as error:
        await run_planner("Add ping endpoint", run_id)

    assert "planner.py: Failed after rate limit retry" in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_uses_provider_abstraction(monkeypatch):
    run_id = str(uuid.uuid4())
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)

    result = await run_planner("Add ping endpoint", run_id)

    assert result.run_id == run_id
    assert llm.roles == [Role.PLANNER]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_llm_request_uses_messages(monkeypatch):
    run_id = str(uuid.uuid4())
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)

    await run_planner("Add ping endpoint", run_id)

    request = llm.requests[0]
    assert request.messages[0].role == "system"
    assert request.messages[0].content == planner.SYSTEM_PROMPT
    assert request.messages[1].role == "user"
    assert "FEATURE REQUEST:\nAdd ping endpoint" in request.messages[1].content
    assert request.model == planner.PLANNER_MODEL
    assert request.temperature == planner.PLANNER_TEMPERATURE
    assert request.max_output_tokens == planner.PLANNER_MAX_TOKENS
    assert request.response_format == "json_object"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_parses_same_response_shape(monkeypatch):
    run_id = str(uuid.uuid4())
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)

    result = await run_planner("Add ping endpoint", run_id)

    assert result.handoff_from == "planner"
    assert result.handoff_to == "coder"
    assert result.goal == "Create a ping endpoint."
    assert result.files_to_modify == ["backend/main.py"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_retry_correction_uses_provider_abstraction(monkeypatch):
    run_id = str(uuid.uuid4())
    llm = _PlannerLLM(["not json", _planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)

    result = await run_planner("Add ping endpoint", run_id)

    assert result.run_id == run_id
    assert llm.roles == [Role.PLANNER, Role.PLANNER]
    correction_request = llm.requests[1]
    assert correction_request.messages[0].role == "system"
    assert correction_request.messages[1].role == "user"
    assert correction_request.messages[2].role == "assistant"
    assert correction_request.messages[2].content == "not json"
    assert correction_request.messages[3].role == "user"
    assert "Your previous response was not valid JSON" in correction_request.messages[3].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_provider_error_does_not_leak_secret(monkeypatch):
    raw_key = "AIzaSyA123456789012345678901234567890"
    llm = _PlannerLLM([
        LLMError(
            f"Provider failed with key {raw_key}",
            provider="gemini",
            model="gemini-2.5-flash-lite",
            retryable=False,
        )
    ])
    _patch_planner_dependencies(monkeypatch, llm)

    with pytest.raises(RuntimeError) as error:
        await run_planner("Add ping endpoint", str(uuid.uuid4()))

    assert raw_key not in str(error.value)
    assert "[REDACTED]" in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_includes_project_memory(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-project-{uuid.uuid4().hex}"
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )
    add_fact(project_id, "Backend uses FastAPI memory", category="stack", scope="backend")

    try:
        await run_planner("Add ping endpoint", run_id, project_id=project_id)
    finally:
        _cleanup_memory(project_id)

    prompt = llm.requests[0].messages[1].content
    assert "=== PROJECT MEMORY" in prompt
    assert "[stack/backend] Backend uses FastAPI memory" in prompt
    assert "source code and explicit user instructions win" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_injection_skips_empty_memory(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-empty-{uuid.uuid4().hex}"
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )

    await run_planner("Add ping endpoint", run_id, project_id=project_id)

    assert "=== PROJECT MEMORY" not in llm.requests[0].messages[1].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_passes_project_id_to_memory_builder(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-spy-{uuid.uuid4().hex}"
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)

    calls = []

    def spy_builder(**kwargs):
        calls.append(kwargs)
        return _empty_memory_result(**kwargs)

    monkeypatch.setattr(planner, "build_project_memory_block_detailed", spy_builder)

    await run_planner("Add ping endpoint", run_id, project_id=project_id)

    assert len(calls) == 1
    assert calls[0]["project_id"] == project_id
    assert calls[0]["role"] == "planner"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_injection_is_project_scoped(monkeypatch):
    run_id = str(uuid.uuid4())
    project_a = f"planner-a-{uuid.uuid4().hex}"
    project_b = f"planner-b-{uuid.uuid4().hex}"
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )
    add_fact(project_a, "Project A planner memory", category="stack")
    add_fact(project_b, "Project B planner memory", category="stack")

    try:
        await run_planner("Add ping endpoint", run_id, project_id=project_a)
    finally:
        _cleanup_memory(project_a)
        _cleanup_memory(project_b)

    prompt = llm.requests[0].messages[1].content
    assert "Project A planner memory" in prompt
    assert "Project B planner memory" not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_prompt_injection_excludes_archived_stale_historical(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"planner-status-{uuid.uuid4().hex}"
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )
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

    prompt = llm.requests[0].messages[1].content
    assert "Active planner memory" in prompt
    assert "Archived planner memory" not in prompt
    assert "Stale planner memory" not in prompt
    assert "Historical planner memory" not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_still_works_without_project_id(monkeypatch):
    run_id = str(uuid.uuid4())
    llm = _PlannerLLM([_planner_response(run_id)])
    _patch_planner_dependencies(monkeypatch, llm)
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )

    result = await run_planner("Add ping endpoint", run_id)

    assert result.run_id == run_id
    assert "=== PROJECT MEMORY" not in llm.requests[0].messages[1].content


@pytest.mark.unit
def test_planner_pipeline_no_longer_imports_gemini_or_uses_print():
    source = Path(planner.__file__).read_text(encoding="utf-8")

    assert "google.generativeai" not in source
    assert "genai" not in source
    assert "print(" not in source


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
