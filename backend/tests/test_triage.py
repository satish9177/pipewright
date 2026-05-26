"""
test_triage.py
Tests for Phase 2B triage.
No API calls. Gemini is mocked.
"""

import json
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from backend.db.database import engine
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
    monkeypatch.setattr(
        triage,
        "_call_gemini",
        lambda prompt, run_id: valid_triage_json(run_id, project_id),
    )

    result = await triage.run_triage(run_id, project_id, "Add chunk planning")

    assert isinstance(result, TriageResult)
    assert result.run_id == run_id
    assert result.project_id == project_id
    assert result.total_chunks == 1


@pytest.mark.asyncio
async def test_malformed_json_retries_once(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    calls = {"count": 0}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    def fake_call(prompt, run_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return "not json"
        return valid_triage_json(run_id, project_id)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

    result = await triage.run_triage(run_id, project_id, "Add chunk planning")

    assert result.total_chunks == 1
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_malformed_twice_raises_runtime_error(monkeypatch, tmp_repo):
    patch_triage_dependencies(monkeypatch, tmp_repo)
    monkeypatch.setattr(triage, "_call_gemini", lambda prompt, run_id: "nope")

    with pytest.raises(RuntimeError):
        await triage.run_triage(str(uuid.uuid4()), "proj-test", "Feature")


@pytest.mark.asyncio
async def test_prompt_includes_core_chunking_rules(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-test"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)

    def fake_call(prompt, run_id):
        captured["prompt"] = prompt
        return valid_triage_json(run_id, project_id)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

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

    def fake_call(prompt, run_id):
        captured["prompt"] = prompt
        return valid_triage_json(run_id, project_id)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

    await triage.run_triage(run_id, project_id, "workflow approvals")

    assert "backend/app/routers/workflows.py" in captured["prompt"]


@pytest.mark.asyncio
async def test_triage_prompt_includes_project_memory(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = f"triage-project-{uuid.uuid4().hex}"
    captured = {}
    patch_triage_dependencies(monkeypatch, tmp_repo)
    add_fact(project_id, "Backend uses FastAPI memory", category="stack", scope="backend")

    def fake_call(prompt, run_id):
        captured["prompt"] = prompt
        return valid_triage_json(run_id, project_id)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

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

    def fake_call(prompt, run_id):
        captured["prompt"] = prompt
        return valid_triage_json(run_id, project_id)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

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

    def fake_call(prompt, run_id):
        captured["prompt"] = prompt
        return valid_triage_json(run_id, project_a)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

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

    def fake_call(prompt, run_id):
        captured["prompt"] = prompt
        return valid_triage_json(run_id, project_id)

    monkeypatch.setattr(triage, "_call_gemini", fake_call)

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
