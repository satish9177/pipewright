"""
test_coder.py
Tests for coder.py pipeline stage.
Mocked unit tests are fast and local.
API-marked tests make real Gemini calls.
The target repo is ai-workflow-platform.
Coder reads files from there but never writes.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from backend.config.keys import settings
from backend.db.database import engine
from backend.llm import retry as llm_retry
from backend.llm.base import LLMResponse
from backend.llm.errors import LLMError
from backend.llm.role_config import Role
from backend.memory.memory_store import add_fact
from backend.memory.prompt_builder import (
    build_project_memory_block_detailed as real_build_memory_block_detailed,
)


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
from backend.models.handoff import PlannerHandoff
from backend.pipeline import coder
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
            "No database calls needed",
        ],
        files_to_create=["backend/routes/health.py"],
        files_to_modify=["backend/main.py"],
        files_to_read=["backend/main.py"],
        out_of_scope=["authentication", "database"],
        risks=["main.py may not exist in target repo"],
        suggested_memory_entries=[],
    )


def _skip_without_gemini_key():
    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY is required for live Gemini test")


def _coder_response(run_id: str):
    return (
        "{"
        '"handoff_from": "coder",'
        '"handoff_to": "patch_applier",'
        f'"run_id": "{run_id}",'
        '"feature_description": "Add a ping endpoint",'
        '"files_changed": ['
        "{"
        '"path": "backend/routes/ping.py",'
        '"action": "create",'
        '"content": "def ping():\\n    return {\'status\': \'ok\'}\\n",'
        '"reason": "Add ping route"'
        "}"
        "],"
        '"summary": "Added a ping route.",'
        '"suggested_memory_entries": []'
        "}"
    )


class _CoderLLM:
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


def _patch_coder_dependencies(monkeypatch, llm, tmp_repo):
    monkeypatch.setattr(
        coder, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(coder, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(coder, "get_target_repo_path", lambda: str(tmp_repo))
    monkeypatch.setattr(coder, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(coder, "complete_for_role", llm.complete)


def _cleanup_memory(project_id: str):
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM memory_facts WHERE project_id = :project_id
        """), {"project_id": project_id})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_429_during_correction_retried_with_bounded_backoff(
    monkeypatch, tmp_repo
):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([
        "{not json",
        RuntimeError("429 rate limit"),
        _coder_response(run_id),
    ])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)
    checkpoint_calls = []
    monkeypatch.setattr(
        coder,
        "save_checkpoint",
        lambda **kwargs: checkpoint_calls.append(kwargs),
    )
    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.run_id == run_id
    assert len(result.files_changed) == 1
    # E4: one bounded backoff (2s base + <=1s jitter), never a 60s
    # lock-held sleep.
    assert len(sleeps) == 1
    assert 2.0 <= sleeps[0] <= 3.0
    assert not hasattr(coder, "time")
    assert llm.roles == [Role.CODER, Role.CODER, Role.CODER]
    assert checkpoint_calls[0]["step"] == "code"
    assert checkpoint_calls[0]["tests_passed"] is False
    assert checkpoint_calls[0]["step_completed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_429_exhaustion_raises_runtime_error(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([
        "{not json",
        RuntimeError("429 rate limit"),
        RuntimeError("429 still rate limited"),
        RuntimeError("429 still rate limited"),
    ])

    async def fake_sleep(seconds):
        return None

    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)
    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError) as error:
        await run_coder(plan=plan, run_id=run_id)

    assert "coder.py: Rate limited; retries exhausted" in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_429_on_first_attempt_is_retried(monkeypatch, tmp_repo):
    # New with E4: a first-attempt rate limit no longer dead-ends the chunk —
    # the shared executor retries it like any other provider call.
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([
        RuntimeError("429 rate limit"),
        _coder_response(run_id),
    ])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)
    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.run_id == run_id
    assert len(sleeps) == 1
    assert llm.roles == [Role.CODER, Role.CODER]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_uses_provider_abstraction(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.run_id == run_id
    assert llm.roles == [Role.CODER]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_llm_request_uses_messages(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    await run_coder(plan=plan, run_id=run_id)

    request = llm.requests[0]
    assert request.messages[0].role == "system"
    assert request.messages[0].content == coder.CODER_SYSTEM_PROMPT
    assert request.messages[1].role == "user"
    assert "IMPLEMENTATION PLAN:" in request.messages[1].content
    # The stage no longer pins a model; complete_for_role resolves it per role.
    assert request.model == ""
    assert request.temperature == coder.CODER_TEMPERATURE
    assert request.max_output_tokens == coder.CODER_MAX_TOKENS
    assert request.response_format == "json_object"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_timeout_is_passed_to_llm_request(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    await run_coder(plan=plan, run_id=run_id)

    assert llm.requests[0].timeout_seconds == coder.CODER_TIMEOUT_SECONDS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_parses_same_response_shape(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"
    assert result.handoff_to == "patch_applier"
    assert result.files_changed[0].path == "backend/routes/ping.py"
    assert result.summary == "Added a ping route."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_retry_correction_uses_provider_abstraction(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM(["{not json", _coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.run_id == run_id
    assert llm.roles == [Role.CODER, Role.CODER]
    correction_request = llm.requests[1]
    assert correction_request.messages[0].role == "system"
    assert correction_request.messages[1].role == "user"
    assert correction_request.messages[2].role == "assistant"
    assert correction_request.messages[2].content == "{not json"
    assert correction_request.messages[3].role == "user"
    assert "Your previous response was not valid JSON" in correction_request.messages[3].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_provider_error_does_not_leak_secret(monkeypatch, tmp_repo):
    raw_key = "AIzaSyA123456789012345678901234567890"
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    llm = _CoderLLM([
        LLMError(
            f"Provider failed with key {raw_key}",
            provider="gemini",
            model="gemini-2.5-flash-lite",
            retryable=False,
        )
    ])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    with pytest.raises(RuntimeError) as error:
        await run_coder(plan=plan, run_id=run_id)

    assert raw_key not in str(error.value)
    assert "[REDACTED]" in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_includes_project_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"coder-project-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)
    monkeypatch.setattr(
        coder, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )
    add_fact(project_id, "Backend uses FastAPI memory", category="stack", scope="backend")

    try:
        await run_coder(plan=plan, run_id=run_id, project_id=project_id)
    finally:
        _cleanup_memory(project_id)

    prompt = llm.requests[0].messages[1].content
    assert "=== PROJECT MEMORY" in prompt
    assert "[stack/backend] Backend uses FastAPI memory" in prompt
    assert "source code and explicit user instructions win" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_injection_skips_empty_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"coder-empty-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)
    monkeypatch.setattr(
        coder, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )

    await run_coder(plan=plan, run_id=run_id, project_id=project_id)

    assert "=== PROJECT MEMORY" not in llm.requests[0].messages[1].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_passes_project_id_to_memory_builder(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"coder-spy-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    calls = []

    def spy_builder(**kwargs):
        calls.append(kwargs)
        return _empty_memory_result(**kwargs)

    monkeypatch.setattr(coder, "build_project_memory_block_detailed", spy_builder)

    await run_coder(plan=plan, run_id=run_id, project_id=project_id)

    assert len(calls) == 1
    assert calls[0]["project_id"] == project_id
    assert calls[0]["role"] == "coder"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_injection_is_project_scoped(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_a = f"coder-a-{uuid.uuid4().hex}"
    project_b = f"coder-b-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)
    monkeypatch.setattr(
        coder, "build_project_memory_block_detailed", real_build_memory_block_detailed
    )
    add_fact(project_a, "Project A coder memory", category="stack")
    add_fact(project_b, "Project B coder memory", category="stack")

    try:
        await run_coder(plan=plan, run_id=run_id, project_id=project_a)
    finally:
        _cleanup_memory(project_a)
        _cleanup_memory(project_b)

    prompt = llm.requests[0].messages[1].content
    assert "Project A coder memory" in prompt
    assert "Project B coder memory" not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_still_contains_file_contents(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    file_path = tmp_repo / "backend" / "main.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("def existing():\n    return 'ok'\n", encoding="utf-8")
    llm = _CoderLLM([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, llm, tmp_repo)

    await run_coder(plan=plan, run_id=run_id)

    prompt = llm.requests[0].messages[1].content
    assert "--- FILE: backend/main.py ---" in prompt
    assert "def existing():" in prompt
    assert "--- END FILE ---" in prompt


@pytest.mark.unit
def test_coder_pipeline_no_longer_imports_gemini_or_uses_print():
    source = Path(coder.__file__).read_text(encoding="utf-8")

    assert "google.generativeai" not in source
    assert "genai" not in source
    assert "print(" not in source


@pytest.mark.api
@pytest.mark.asyncio
async def test_coder_returns_valid_handoff():
    _skip_without_gemini_key()
    project_id = str(uuid.uuid4())
    add_fact(
        project_id,
        "Tech stack: Python FastAPI",
        source="test", added_by="founder",
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
        assert fc.action in ["create", "modify", "delete", "edit"]
        assert fc.path and len(fc.path) > 0
        assert fc.reason and len(fc.reason) > 0
        if fc.action in ["create", "modify"]:
            assert fc.content is not None
            assert len(fc.content) > 0
        if fc.action == "edit":
            assert fc.old_string is not None
            assert fc.new_string is not None


@pytest.mark.api
@pytest.mark.asyncio
async def test_coder_handles_missing_files_gracefully():
    _skip_without_gemini_key()
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
        suggested_memory_entries=[],
    )

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"
    assert len(result.files_changed) > 0
