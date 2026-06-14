"""
test_memory_run_suggestions_readmodel.py
Read-only run memory suggestion digest tests.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.memory.bootstrap import (
    approve_suggestion,
    insert_pending_suggestion,
    reject_suggestion,
)
from backend.routes import memory as memory_routes

pytestmark = pytest.mark.unit


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def run_env(client, tmp_repo):
    project_ids: list[str] = []
    run_ids: list[str] = []

    def create_project() -> str:
        response = client.post("/projects", json={
            "name": f"RunSuggestionDigest {uuid.uuid4()}",
            "repo_path": str(tmp_repo / str(uuid.uuid4())),
            "test_command": "python -m pytest -q",
        })
        assert response.status_code == 200
        project_id = response.json()["id"]
        project_ids.append(project_id)
        return project_id

    def create_run(project_id: str, *, status: str = "complete") -> str:
        run_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_runs
                    (id, project_id, feature_description, status)
                VALUES
                    (:id, :project_id, :feature_description, :status)
            """), {
                "id": run_id,
                "project_id": project_id,
                "feature_description": "Digest read model test",
                "status": status,
            })
        run_ids.append(run_id)
        return run_id

    env = type("RunEnv", (), {})()
    env.create_project = create_project
    env.create_run = create_run
    yield env

    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {
                "run_id": run_id,
            })
        for project_id in project_ids:
            conn.execute(text(
                "DELETE FROM memory_suggestions WHERE project_id = :project_id"
            ), {"project_id": project_id})
            conn.execute(text(
                "DELETE FROM memory_facts WHERE project_id = :project_id"
            ), {"project_id": project_id})
            conn.execute(text("DELETE FROM projects WHERE id = :project_id"), {
                "project_id": project_id,
            })


def _digest_url(run_id: str) -> str:
    return f"/api/v1/runs/{run_id}/memory-suggestions"


def _add_pending_suggestion(
    project_id: str,
    run_id: str,
    content: str,
) -> dict:
    suggestion = insert_pending_suggestion(
        project_id,
        content,
        category="test",
        scope="global",
        priority=100,
        source="run_outcome",
        source_type="run_handoff",
        source_run_id=run_id,
        source_chunk_number=1,
        rationale=f"Read-model test suggestion for run {run_id}.",
        suggested_by="test",
        risk_level="low",
    )
    assert suggestion is not None
    return suggestion


def _count_project_suggestions(project_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("""
            SELECT COUNT(*)
            FROM memory_suggestions
            WHERE project_id = :project_id
        """), {"project_id": project_id}).scalar_one())


def _count_project_active_facts(project_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("""
            SELECT COUNT(*)
            FROM memory_facts
            WHERE project_id = :project_id
              AND status = 'active'
        """), {"project_id": project_id}).scalar_one())


def _count_run_approval_gates(run_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("""
            SELECT COUNT(*)
            FROM approval_gates
            WHERE run_id = :run_id
        """), {"run_id": run_id}).scalar_one())


def test_missing_run_returns_404(client):
    response = client.get(_digest_url("does-not-exist"))

    assert response.status_code == 404


def test_existing_run_with_no_pending_suggestions_returns_empty(client, run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id)

    response = client.get(_digest_url(run_id))

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "project_id": project_id,
        "pending_count": 0,
        "suggestions": [],
    }


def test_digest_filters_pending_suggestions_to_this_run_only(client, run_env):
    project_id = run_env.create_project()
    run_a = run_env.create_run(project_id)
    run_b = run_env.create_run(project_id)
    suggestion_a = _add_pending_suggestion(
        project_id,
        run_a,
        "Run A suggests pytest remains the project test runner.",
    )
    suggestion_b = _add_pending_suggestion(
        project_id,
        run_b,
        "Run B suggests handoff notes stay advisory.",
    )

    response_a = client.get(_digest_url(run_a))
    response_b = client.get(_digest_url(run_b))

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    body_a = response_a.json()
    body_b = response_b.json()
    assert body_a["pending_count"] == 1
    assert body_b["pending_count"] == 1
    assert [suggestion["id"] for suggestion in body_a["suggestions"]] == [
        suggestion_a["id"]
    ]
    assert [suggestion["source_run_id"] for suggestion in body_a["suggestions"]] == [
        run_a
    ]
    assert [suggestion["id"] for suggestion in body_b["suggestions"]] == [
        suggestion_b["id"]
    ]
    assert [suggestion["source_run_id"] for suggestion in body_b["suggestions"]] == [
        run_b
    ]


def test_digest_excludes_non_pending_suggestions_for_same_run(client, run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id)
    pending = _add_pending_suggestion(
        project_id,
        run_id,
        "Pending suggestion should be visible in the digest.",
    )
    approved = _add_pending_suggestion(
        project_id,
        run_id,
        "Approved suggestion should not be visible in the digest.",
    )
    rejected = _add_pending_suggestion(
        project_id,
        run_id,
        "Rejected suggestion should not be visible in the digest.",
    )
    approve_suggestion(project_id, approved["id"], approved_by="test")
    reject_suggestion(project_id, rejected["id"], reason="Not useful here.")

    response = client.get(_digest_url(run_id))

    assert response.status_code == 200
    body = response.json()
    assert body["pending_count"] == 1
    assert [suggestion["id"] for suggestion in body["suggestions"]] == [pending["id"]]
    assert body["suggestions"][0]["status"] == "pending"


def test_digest_read_is_non_mutating_and_never_generates(
    client,
    run_env,
    monkeypatch,
):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id)
    _add_pending_suggestion(
        project_id,
        run_id,
        "Reading the digest must not create suggestions or facts.",
    )
    monkeypatch.setattr(
        memory_routes,
        "generate_run_memory_suggestions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GET digest must not call the generator")
        ),
    )
    before_suggestions = _count_project_suggestions(project_id)
    before_facts = _count_project_active_facts(project_id)
    before_gates = _count_run_approval_gates(run_id)

    first = client.get(_digest_url(run_id))
    second = client.get(_digest_url(run_id))

    assert first.status_code == 200
    assert second.status_code == 200
    assert _count_project_suggestions(project_id) == before_suggestions
    assert _count_project_active_facts(project_id) == before_facts == 0
    assert _count_run_approval_gates(run_id) == before_gates == 0


def test_digest_response_uses_safe_suggestion_shape(client, run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id)
    suggestion = _add_pending_suggestion(
        project_id,
        run_id,
        "Frontend can render this pending suggestion card.",
    )

    response = client.get(_digest_url(run_id))

    assert response.status_code == 200
    body = response.json()
    returned = body["suggestions"][0]
    assert body["pending_count"] == 1
    assert returned["id"] == suggestion["id"]
    assert returned["content"] == suggestion["content"]
    assert returned["category"] == "test"
    assert returned["scope"] == "global"
    assert returned["priority"] == 100
    assert returned["status"] == "pending"
    assert returned["source_run_id"] == run_id
    assert returned["source_type"] == "run_handoff"
    assert "content_hash" not in returned
    assert "content_hash" not in body
