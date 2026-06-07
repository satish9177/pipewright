"""
test_start_branch_guard_routes.py
#34D1 run-creation guard for unsafe start branches.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.git.local_git import StartBranchInspection
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.clarification_context import (
    create_clarification_context,
    encode_clarification_context,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids = []
    yield run_ids
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


def make_project(tmp_repo):
    return create_project(
        name=f"Start Branch Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def seed_file_index(project_id: str, paths: list[str]) -> None:
    with engine.begin() as conn:
        for path in paths:
            conn.execute(text("""
                INSERT INTO file_index
                (id, project_id, path, file_type, summary, key_imports,
                 last_modified, token_estimate, line_count, size_bytes)
                VALUES
                (:id, :project_id, :path, 'unknown', NULL, '[]',
                 NULL, 100, 10, 100)
            """), {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "path": path,
            })


def count_runs(project_id: str) -> int:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COUNT(*) FROM pipeline_runs WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchone()
    return int(row[0])


def make_triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="update app.py to print hello",
        complexity="easy",
        total_chunks=1,
        reasoning="One chunk is enough.",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Update app",
            description="Update the app behavior.",
            files_expected=["app.py"],
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="Small change.",
        )],
    )


def freshness_model() -> dict:
    return {
        "state": "current",
        "reasons": [],
        "current": None,
        "indexed": None,
        "index_row_count": 1,
        "has_index_rows": True,
        "has_snapshot": True,
    }


def install_inspection(monkeypatch, inspection: StartBranchInspection) -> None:
    monkeypatch.setattr(
        "backend.routes.chunks.inspect_start_branch",
        lambda _repo_path: inspection,
    )


def install_fake_triage(monkeypatch) -> None:
    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)


def forbid_later_run_creation_reads(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("freshness must not run after unsafe start branch")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.get_indexed_paths_and_dirs",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("index must not be read after unsafe start branch")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("triage must not run after unsafe start branch")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run creation must not happen after unsafe start branch")
        ),
    )


def assert_unsafe_start_branch_response(
    data: dict,
    *,
    current_branch: str | None,
    message_text: str,
) -> None:
    assert data["status"] == "unsafe_start_branch"
    assert data["outcome"] == "unsafe_start_branch"
    assert data["run_created"] is False
    assert message_text in data["message"]
    assert data["current_branch"] == current_branch
    assert data["current_head_sha_short"] == "abcdef123456"
    assert data["recommended_action"] == "checkout_start_branch"
    assert "run_id" not in data


def test_implementation_creation_on_pipewright_branch_returns_no_run(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    install_inspection(
        monkeypatch,
        StartBranchInspection(
            current_branch="pipewright/old-run",
            head_sha_short="abcdef123456",
        ),
    )
    forbid_later_run_creation_reads(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    assert_unsafe_start_branch_response(
        response.json(),
        current_branch="pipewright/old-run",
        message_text="Pipewright run branch",
    )
    assert count_runs(project["id"]) == 0


def test_implementation_creation_on_detached_head_returns_no_run(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    install_inspection(
        monkeypatch,
        StartBranchInspection(
            current_branch=None,
            head_sha_short="abcdef123456",
            is_detached=True,
        ),
    )
    forbid_later_run_creation_reads(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    assert_unsafe_start_branch_response(
        response.json(),
        current_branch=None,
        message_text="detached HEAD",
    )
    assert count_runs(project["id"]) == 0


def test_implementation_creation_on_normal_branch_proceeds(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py"])
    install_inspection(
        monkeypatch,
        StartBranchInspection(
            current_branch="feature/start",
            head_sha_short="abcdef123456",
        ),
    )
    install_fake_triage(monkeypatch)
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: freshness_model(),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "unsafe_start_branch"
    assert data["chunk_plan_status"] == "awaiting_approval"


def test_report_only_on_pipewright_branch_is_not_guarded(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.inspect_start_branch",
        lambda _repo_path: (_ for _ in ()).throw(
            AssertionError("report-only requests must not inspect start branch")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("report-only requests must not triage")
        ),
    )

    class FakeAnalysis:
        markdown_report = "Read-only report"
        report_result = None

    async def fake_report_analysis(**kwargs):
        return FakeAnalysis()

    monkeypatch.setattr(
        "backend.routes.chunks.run_report_analysis",
        fake_report_analysis,
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "find bugs in the codebase",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "none"
    assert data["triage"] is None


def test_clarification_selection_reentry_uses_start_branch_guard(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    context = create_clarification_context(
        project_id=project["id"],
        original_feature_description="update README.md to add install instructions",
        alias="README.md",
        candidates=["README.md"],
        recommended_path="README.md",
        recommendation_strength="high",
        now=datetime.now(timezone.utc),
    )
    token = encode_clarification_context(context)
    install_inspection(
        monkeypatch,
        StartBranchInspection(
            current_branch="pipewright/old-run",
            head_sha_short="abcdef123456",
        ),
    )
    forbid_later_run_creation_reads(monkeypatch)
    client = TestClient(app)

    response = client.post(
        f"/runs/chunked/clarifications/{token}/select",
        json={"project_id": project["id"], "selection": "1"},
    )

    assert response.status_code == 200
    assert_unsafe_start_branch_response(
        response.json(),
        current_branch="pipewright/old-run",
        message_text="Pipewright run branch",
    )
    assert count_runs(project["id"]) == 0
