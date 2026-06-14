"""
End-to-end readiness checks for dormant request-aware relevance omission.

These tests monkeypatch the global omission flag on only inside test functions.
They drive the real coder memory builder and real memory-injection capture so
the persisted provenance contract is exercised before any later activation.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.llm.base import LLMResponse
from backend.main import app
from backend.memory.injection_store import list_memory_injection_events
from backend.memory.memory_store import add_fact
from backend.memory.prompt_builder import (
    EXCLUSION_BUDGET_DROPPED,
    EXCLUSION_NOT_RELEVANT_TO_REQUEST,
)
from backend.models.handoff import PlannerHandoff
from backend.pipeline import coder, policy
from backend.pipeline.coder import run_coder

pytestmark = pytest.mark.unit


def _make_plan(run_id: str) -> PlannerHandoff:
    return PlannerHandoff(
        run_id=run_id,
        feature_description="Update the frontend App rendering.",
        goal="Update the frontend App view",
        steps=["Create the App component", "Keep the rendering simple"],
        files_to_create=["frontend/src/App.tsx"],
        files_to_modify=[],
        files_to_read=[],
        out_of_scope=["backend routes"],
        risks=[],
        suggested_memory_entries=[],
    )


def _coder_json(run_id: str) -> str:
    return json.dumps({
        "handoff_from": "coder",
        "handoff_to": "patch_applier",
        "run_id": run_id,
        "feature_description": "Update the frontend App rendering.",
        "files_changed": [
            {
                "path": "frontend/src/App.tsx",
                "action": "create",
                "content": "export default function App() { return <main /> }\n",
                "reason": "Create the requested App component.",
            }
        ],
        "summary": "Created the App component.",
        "suggested_memory_entries": [],
    })


class _FakeLLM:
    def __init__(self, text_out: str):
        self.text_out = text_out

    async def complete(self, role, request):
        return LLMResponse(
            text=self.text_out,
            provider="fake",
            model="memory-omission-test",
            input_tokens=1,
            output_tokens=1,
            finish_reason="stop",
        )


def _patch_coder_memory_live(monkeypatch, llm: _FakeLLM, tmp_repo) -> None:
    monkeypatch.setattr(coder, "get_target_repo_path", lambda: str(tmp_repo))
    monkeypatch.setattr(coder, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(coder, "complete_for_role", llm.complete)


def _enable_omission(monkeypatch) -> None:
    monkeypatch.setattr(policy, "MEMORY_RELEVANCE_OMISSION_ENABLED", True)


def _seed(project_id: str) -> dict:
    security = add_fact(
        project_id,
        "Never leak tokens.",
        category="security",
        priority=100,
    )
    forbidden = add_fact(
        project_id,
        "Keep secret files untouched.",
        category="forbidden_paths",
        priority=100,
    )
    pinned = add_fact(
        project_id,
        "Pinned style guidance always applies.",
        category="style",
        priority=5,
    )
    signal = add_fact(
        project_id,
        "Frontend App tsx rendering stays simple.",
        category="style",
        priority=100,
    )
    zeros = [
        add_fact(
            project_id,
            f"Database migration note number {index} stays small.",
            category="style",
            priority=100,
        )
        for index in range(13)
    ]
    return {
        "security": security,
        "forbidden": forbidden,
        "pinned": pinned,
        "signal": signal,
        "zeros": zeros,
    }


def _cleanup_project(project_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM memory_facts WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        conn.execute(
            text(
                "DELETE FROM memory_injection_events "
                "WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        )


def _insert_run(run_id: str, project_id: str) -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO pipeline_runs
                    (id, project_id, feature_description, status)
                VALUES (:id, :project_id, :feature, 'running')
            """),
            {
                "id": run_id,
                "project_id": project_id,
                "feature": "memory relevance omission readiness",
            },
        )


def _delete_run(run_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM pipeline_runs WHERE id = :id"),
            {"id": run_id},
        )
        conn.execute(
            text("DELETE FROM memory_injection_events WHERE run_id = :id"),
            {"id": run_id},
        )
        conn.execute(
            text("DELETE FROM llm_call_provenance WHERE run_id = :id"),
            {"id": run_id},
        )


def _event_for(run_id: str, project_id: str, chunk_number: int) -> dict:
    events = list_memory_injection_events(
        run_id,
        project_id=project_id,
        role="coder",
        chunk_number=chunk_number,
    )
    assert len(events) == 1
    return events[0]


async def _run_coder_with_memory(
    monkeypatch,
    tmp_repo,
    *,
    run_id: str,
    project_id: str,
    chunk_number: int,
):
    llm = _FakeLLM(_coder_json(run_id))
    _patch_coder_memory_live(monkeypatch, llm, tmp_repo)
    return await run_coder(
        plan=_make_plan(run_id),
        run_id=run_id,
        chunk_number=chunk_number,
        project_id=project_id,
    )


@pytest.mark.asyncio
async def test_coder_omits_not_relevant_above_grace_and_persists(
    monkeypatch,
    tmp_repo,
):
    _enable_omission(monkeypatch)
    run_id = f"mroi-{uuid.uuid4().hex}"
    project_id = f"mroip-{uuid.uuid4().hex}"
    facts = _seed(project_id)

    try:
        await _run_coder_with_memory(
            monkeypatch,
            tmp_repo,
            run_id=run_id,
            project_id=project_id,
            chunk_number=1,
        )
        event = _event_for(run_id, project_id, chunk_number=1)
        excluded = event["excluded_entries"]

        assert {
            entry["content"]
            for entry in excluded
            if entry["exclusion_reason"] == EXCLUSION_NOT_RELEVANT_TO_REQUEST
        } == {zero["content"] for zero in facts["zeros"]}
        assert facts["signal"]["content"] in {
            entry["content"] for entry in event["included_entries"]
        }
        assert EXCLUSION_BUDGET_DROPPED not in {
            entry["exclusion_reason"] for entry in excluded
        }
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


@pytest.mark.asyncio
async def test_coder_safety_and_pinned_survive_omission(monkeypatch, tmp_repo):
    _enable_omission(monkeypatch)
    run_id = f"mroi-{uuid.uuid4().hex}"
    project_id = f"mroip-{uuid.uuid4().hex}"
    facts = _seed(project_id)

    try:
        await _run_coder_with_memory(
            monkeypatch,
            tmp_repo,
            run_id=run_id,
            project_id=project_id,
            chunk_number=2,
        )
        event = _event_for(run_id, project_id, chunk_number=2)
        included = {entry["content"] for entry in event["included_entries"]}
        omitted = {
            entry["content"]
            for entry in event["excluded_entries"]
            if entry["exclusion_reason"] == EXCLUSION_NOT_RELEVANT_TO_REQUEST
        }

        assert facts["security"]["content"] in included
        assert facts["forbidden"]["content"] in included
        assert facts["pinned"]["content"] in included
        assert facts["security"]["content"] not in omitted
        assert facts["forbidden"]["content"] not in omitted
        assert facts["pinned"]["content"] not in omitted
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


@pytest.mark.asyncio
async def test_coder_flag_off_persists_no_omission(monkeypatch, tmp_repo):
    run_id = f"mroi-{uuid.uuid4().hex}"
    project_id = f"mroip-{uuid.uuid4().hex}"
    _seed(project_id)

    try:
        await _run_coder_with_memory(
            monkeypatch,
            tmp_repo,
            run_id=run_id,
            project_id=project_id,
            chunk_number=3,
        )
        event = _event_for(run_id, project_id, chunk_number=3)

        assert [
            entry
            for entry in event["excluded_entries"]
            if entry["exclusion_reason"] == EXCLUSION_NOT_RELEVANT_TO_REQUEST
        ] == []
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


@pytest.mark.asyncio
async def test_omitted_entries_reach_read_endpoint(monkeypatch, tmp_repo):
    _enable_omission(monkeypatch)
    client = TestClient(app)
    run_id = f"mroi-{uuid.uuid4().hex}"
    project_id = f"mroip-{uuid.uuid4().hex}"
    facts = _seed(project_id)
    _insert_run(run_id, project_id)

    try:
        await _run_coder_with_memory(
            monkeypatch,
            tmp_repo,
            run_id=run_id,
            project_id=project_id,
            chunk_number=4,
        )

        response = client.get(
            f"/api/v1/runs/{run_id}/memory-injections",
            params={"role": "coder"},
        )

        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) == 1
        event = events[0]
        assert {
            entry["content"]
            for entry in event["excluded_entries"]
            if entry["exclusion_reason"] == EXCLUSION_NOT_RELEVANT_TO_REQUEST
        } == {zero["content"] for zero in facts["zeros"]}
        included = {entry["content"] for entry in event["included_entries"]}
        assert facts["security"]["content"] in included
        assert facts["forbidden"]["content"] in included
        assert facts["pinned"]["content"] in included
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)
