"""
test_triage.py
Tests for Phase 2B triage.
No API calls. Gemini is mocked.
"""

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from backend.db.database import engine
from backend.llm.base import LLMResponse
from backend.llm.errors import LLMError
from backend.llm.role_config import Role
from backend.memory.memory_store import add_fact, archive_fact
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import triage

pytestmark = pytest.mark.unit


def valid_triage_json(run_id: str, project_id: str) -> str:
    return json.dumps({
        "run_id": run_id,
        "project_id": project_id,
        "feature_description": "Add chunk planning",
        "complexity": "easy",
        "total_chunks": 1,
        "reasoning": "One safe chunk is enough.",
        "chunks": [{
            "chunk_number": 1,
            "title": "Add planning route",
            "description": "Create chunk planning endpoint.",
            "files_expected": ["backend/routes/chunks.py"],
            "depends_on": [],
            "risk_level": "low",
            "token_estimate": 1000,
            "requires_human_review": False,
            "rationale": "Small isolated API surface.",
        }],
    })


def patch_triage_dependencies(monkeypatch, tmp_repo, relevant_files=None):
    monkeypatch.setattr(
        triage,
        "require_project",
        lambda project_id: {
            "id": project_id,
            "name": "Test Project",
            "repo_path": str(tmp_repo),
        },
    )
    monkeypatch.setattr(
        triage,
        "ensure_repo_indexed",
        lambda project_id, repo_path: {"status": "already_indexed"},
    )
    monkeypatch.setattr(
        triage,
        "get_relevant_files",
        lambda project_id, query, limit=20: relevant_files or [],
    )


def patch_triage_llm(monkeypatch, responder):
    async def fake_complete_for_role(role, request):
        return LLMResponse(
            text=responder(role, request),
            provider="fake",
            model=request.model,
            input_tokens=10,
            output_tokens=5,
            finish_reason="stop",
        )

    monkeypatch.setattr(triage, "complete_for_role", fake_complete_for_role)


def cleanup_memory(project_id: str):
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM memory_facts WHERE project_id = :project_id
        """), {"project_id": project_id})


@pytest.mark.asyncio
async def test_valid_mocked_ai_json_parses_into_triage_result(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    patch_triage_dependencies(monkeypatch, tmp_repo)
    patch_triage_llm(
        monkeypatch,
        lambda role, request: valid_triage_json(run_id, project_id),
    )

    result = await triage.run_triage(run_id, project_id, "Add chunk planning")

    assert isinstance(result, TriageResult)
    assert result.run_id == run_id
    assert result.project_id == project_id
    assert result.total_chunks == 1


@pytest.mark.asyncio
async def test_triage_uses_provider_abstraction(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    async def fake_complete_for_role(role, request):
        captured["role"] = role
        captured["request"] = request
        return LLMResponse(
            text=valid_triage_json(run_id, project_id),
            provider="fake",
            model=request.model,
        )

    monkeypatch.setattr(triage, "complete_for_role", fake_complete_for_role)

    result = await triage.run_triage(run_id, project_id, "Add chunk planning")

    assert result.total_chunks == 1
    assert captured["role"] == Role.TRIAGE


@pytest.mark.asyncio
async def test_triage_llm_request_uses_messages(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    def fake_call(role, request):
        captured["request"] = request
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    await triage.run_triage(run_id, project_id, "Add chunk planning")

    request = captured["request"]
    assert request.messages[0].role == "system"
    assert request.messages[0].content == triage.TRIAGE_SYSTEM_PROMPT
    assert request.messages[1].role == "user"
    assert "FEATURE REQUEST:\nAdd chunk planning" in request.messages[1].content
    assert request.model == triage.TRIAGE_MODEL
    assert request.temperature == triage.TRIAGE_TEMPERATURE
    assert request.max_output_tokens == triage.TRIAGE_MAX_TOKENS
    assert request.response_format == "json_object"


@pytest.mark.asyncio
async def test_malformed_json_retries_once(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    calls = {"count": 0}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    def fake_call(role, request):
        calls["count"] += 1
        if calls["count"] == 1:
            return "not json"
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    result = await triage.run_triage(run_id, project_id, "Add chunk planning")

    assert result.total_chunks == 1
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_malformed_twice_raises_runtime_error(monkeypatch, tmp_repo):
    patch_triage_dependencies(monkeypatch, tmp_repo)
    patch_triage_llm(monkeypatch, lambda role, request: "nope")

    with pytest.raises(RuntimeError):
        await triage.run_triage(str(uuid.uuid4()), "proj-test", "Feature")


@pytest.mark.asyncio
async def test_triage_provider_error_does_not_leak_secret(monkeypatch, tmp_repo):
    raw_key = "AIzaSyA123456789012345678901234567890"
    patch_triage_dependencies(monkeypatch, tmp_repo)

    async def fake_complete_for_role(role, request):
        raise LLMError(
            f"Provider failed with key {raw_key}",
            provider="gemini",
            model="gemini-2.5-flash-lite",
            retryable=False,
        )

    monkeypatch.setattr(triage, "complete_for_role", fake_complete_for_role)

    with pytest.raises(RuntimeError) as exc_info:
        await triage.run_triage(str(uuid.uuid4()), "proj-test", "Feature")

    assert raw_key not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prompt_includes_core_chunking_rules(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    def fake_call(role, request):
        captured["prompt"] = request.messages[1].content
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    await triage.run_triage(run_id, project_id, "Add chunk planning")

    prompt = captured["prompt"]
    assert "Each chunk must be independently testable" in prompt
    assert "Dependencies flow forward only" in prompt
    assert "DB migrations must always be isolated" in prompt
    assert "Security/auth/permissions/encryption" in prompt
    assert "fit context window using token_estimate" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_relevant_file_index_paths(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    captured = {}
    patch_triage_dependencies(
        monkeypatch,
        tmp_repo,
        relevant_files=[{
            "path": "backend/app/routers/workflows.py",
            "file_type": "route",
            "token_estimate": 123,
        }],
    )

    def fake_call(role, request):
        captured["prompt"] = request.messages[1].content
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    await triage.run_triage(run_id, project_id, "workflow approvals")

    assert "backend/app/routers/workflows.py" in captured["prompt"]


@pytest.mark.asyncio
async def test_triage_prompt_includes_project_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"triage-project-{uuid.uuid4().hex}"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)
    add_fact(project_id, "Backend uses FastAPI memory", category="stack", scope="backend")

    def fake_call(role, request):
        captured["prompt"] = request.messages[1].content
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    try:
        await triage.run_triage(run_id, project_id, "Add chunk planning")
    finally:
        cleanup_memory(project_id)

    assert "=== PROJECT MEMORY" in captured["prompt"]
    assert "[stack/backend] Backend uses FastAPI memory" in captured["prompt"]
    assert "source code and explicit user instructions win" in captured["prompt"]


@pytest.mark.asyncio
async def test_triage_prompt_injection_skips_empty_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"triage-empty-{uuid.uuid4().hex}"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    def fake_call(role, request):
        captured["prompt"] = request.messages[1].content
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    await triage.run_triage(run_id, project_id, "Add chunk planning")

    assert "=== PROJECT MEMORY" not in captured["prompt"]


@pytest.mark.asyncio
async def test_triage_prompt_injection_is_project_scoped(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_a = f"triage-a-{uuid.uuid4().hex}"
    project_b = f"triage-b-{uuid.uuid4().hex}"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)
    add_fact(project_a, "Project A triage memory", category="stack")
    add_fact(project_b, "Project B triage memory", category="stack")

    def fake_call(role, request):
        captured["prompt"] = request.messages[1].content
        return valid_triage_json(run_id, project_a)

    patch_triage_llm(monkeypatch, fake_call)

    try:
        await triage.run_triage(run_id, project_a, "Add chunk planning")
    finally:
        cleanup_memory(project_a)
        cleanup_memory(project_b)

    assert "Project A triage memory" in captured["prompt"]
    assert "Project B triage memory" not in captured["prompt"]


@pytest.mark.asyncio
async def test_triage_prompt_injection_excludes_archived_stale_historical(
    monkeypatch,
    tmp_repo,
):
    run_id = str(uuid.uuid4())
    project_id = f"triage-status-{uuid.uuid4().hex}"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)
    add_fact(project_id, "Active triage memory", category="stack")
    archived = add_fact(project_id, "Archived triage memory", category="stack")
    stale = add_fact(project_id, "Stale triage memory", category="stack")
    historical = add_fact(project_id, "Historical triage memory", category="stack")
    archive_fact(project_id, archived["id"], "No longer true")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts SET status = 'stale', is_stale = 1 WHERE id = :id
        """), {"id": stale["id"]})
        conn.execute(text("""
            UPDATE memory_facts SET status = 'historical' WHERE id = :id
        """), {"id": historical["id"]})

    def fake_call(role, request):
        captured["prompt"] = request.messages[1].content
        return valid_triage_json(run_id, project_id)

    patch_triage_llm(monkeypatch, fake_call)

    try:
        await triage.run_triage(run_id, project_id, "Add chunk planning")
    finally:
        cleanup_memory(project_id)

    prompt = captured["prompt"]
    assert "Active triage memory" in prompt
    assert "Archived triage memory" not in prompt
    assert "Stale triage memory" not in prompt
    assert "Historical triage memory" not in prompt


def test_high_risk_chunk_requires_human_review_validation():
    with pytest.raises(ValidationError):
        ChunkDefinition(
            chunk_number=1,
            title="Security",
            description="Change auth",
            files_expected=[],
            depends_on=[],
            risk_level="high",
            token_estimate=100,
            requires_human_review=False,
            rationale="Sensitive change",
        )


def test_dependencies_must_flow_forward_validation():
    with pytest.raises(ValidationError):
        ChunkDefinition(
            chunk_number=2,
            title="Bad dependency",
            description="Depends on the future",
            files_expected=[],
            depends_on=[2],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="Invalid",
        )


def test_total_chunks_mismatch_fails_validation():
    with pytest.raises(ValidationError):
        TriageResult(
            run_id="run",
            project_id="proj",
            feature_description="Feature",
            complexity="medium",
            total_chunks=2,
            reasoning="Mismatch",
            chunks=[ChunkDefinition(
                chunk_number=1,
                title="Only chunk",
                description="Only one chunk",
                files_expected=[],
                depends_on=[],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="Mismatch",
            )],
        )


@pytest.mark.unit
def test_triage_pipeline_no_longer_imports_gemini_or_uses_print():
    triage_path = Path(triage.__file__)
    source = triage_path.read_text(encoding="utf-8")

    assert "google.generativeai" not in source
    assert "genai" not in source
    assert "print(" not in source
