"""
test_coder.py
Tests for coder.py pipeline stage.
Mocked unit tests are fast and local.
API-marked tests make real Gemini calls.
The target repo is ai-workflow-platform.
Coder reads files from there but never writes.
"""

import uuid
import pytest
from types import SimpleNamespace
from sqlalchemy import text
from backend.config.keys import settings
from backend.db.database import engine
from backend.memory.memory_store import add_fact
from backend.memory.prompt_builder import build_project_memory_block as real_build_memory_block
from backend.models.handoff import PlannerHandoff
from backend.pipeline.coder import run_coder
from backend.pipeline import coder


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


def _skip_without_gemini_key():
    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY is required for live Gemini test")


def _coder_response(run_id: str):
    text = (
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
    usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=20)
    return SimpleNamespace(text=text, usage_metadata=usage)


class _CoderModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def generate_content(self, prompt, request_options=None):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _patch_coder_dependencies(monkeypatch, model, tmp_repo):
    monkeypatch.setattr(coder, "build_project_memory_block", lambda **kwargs: "")
    monkeypatch.setattr(coder, "get_target_repo_path", lambda: str(tmp_repo))
    monkeypatch.setattr(coder, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(coder.genai, "configure", lambda api_key: None)
    monkeypatch.setattr(
        coder.genai,
        "GenerationConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        coder.genai,
        "GenerativeModel",
        lambda **kwargs: model,
    )


def _cleanup_memory(project_id: str):
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM memory_facts WHERE project_id = :project_id
        """), {"project_id": project_id})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_429_retry_path_does_not_crash(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    model = _CoderModel([
        SimpleNamespace(text="{not json", usage_metadata=SimpleNamespace()),
        RuntimeError("429 rate limit"),
        _coder_response(run_id),
    ])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _patch_coder_dependencies(monkeypatch, model, tmp_repo)
    checkpoint_calls = []
    monkeypatch.setattr(
        coder,
        "save_checkpoint",
        lambda **kwargs: checkpoint_calls.append(kwargs),
    )
    monkeypatch.setattr(coder.asyncio, "sleep", fake_sleep)

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.run_id == run_id
    assert len(result.files_changed) == 1
    assert sleeps == [60]
    assert not hasattr(coder, "time")
    assert checkpoint_calls[0]["step"] == "code"
    assert checkpoint_calls[0]["tests_passed"] is False
    assert checkpoint_calls[0]["step_completed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_429_retry_failure_raises_runtime_error(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = make_test_plan(run_id)
    model = _CoderModel([
        SimpleNamespace(text="{not json", usage_metadata=SimpleNamespace()),
        RuntimeError("429 rate limit"),
        RuntimeError("still rate limited"),
    ])

    async def fake_sleep(seconds):
        return None

    _patch_coder_dependencies(monkeypatch, model, tmp_repo)
    monkeypatch.setattr(coder.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError) as error:
        await run_coder(plan=plan, run_id=run_id)

    assert "coder.py: Failed after rate limit retry" in str(error.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_includes_project_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"coder-project-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    model = _CoderModel([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, model, tmp_repo)
    monkeypatch.setattr(coder, "build_project_memory_block", real_build_memory_block)
    add_fact(project_id, "Backend uses FastAPI memory", category="stack", scope="backend")

    try:
        await run_coder(plan=plan, run_id=run_id, project_id=project_id)
    finally:
        _cleanup_memory(project_id)

    prompt = model.prompts[0]
    assert "=== PROJECT MEMORY" in prompt
    assert "[stack/backend] Backend uses FastAPI memory" in prompt
    assert "source code and explicit user instructions win" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_injection_skips_empty_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"coder-empty-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    model = _CoderModel([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, model, tmp_repo)
    monkeypatch.setattr(coder, "build_project_memory_block", real_build_memory_block)

    await run_coder(plan=plan, run_id=run_id, project_id=project_id)

    assert "=== PROJECT MEMORY" not in model.prompts[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coder_prompt_injection_is_project_scoped(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_a = f"coder-a-{uuid.uuid4().hex}"
    project_b = f"coder-b-{uuid.uuid4().hex}"
    plan = make_test_plan(run_id)
    model = _CoderModel([_coder_response(run_id)])
    _patch_coder_dependencies(monkeypatch, model, tmp_repo)
    monkeypatch.setattr(coder, "build_project_memory_block", real_build_memory_block)
    add_fact(project_a, "Project A coder memory", category="stack")
    add_fact(project_b, "Project B coder memory", category="stack")

    try:
        await run_coder(plan=plan, run_id=run_id, project_id=project_a)
    finally:
        _cleanup_memory(project_a)
        _cleanup_memory(project_b)

    assert "Project A coder memory" in model.prompts[0]
    assert "Project B coder memory" not in model.prompts[0]


@pytest.mark.api
@pytest.mark.asyncio
async def test_coder_returns_valid_handoff():
    _skip_without_gemini_key()
    project_id = str(uuid.uuid4())
    add_fact(
        project_id,
        "Tech stack: Python FastAPI",
        source="test", added_by="founder"
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
        suggested_memory_entries=[]
    )

    result = await run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"
    assert len(result.files_changed) > 0
