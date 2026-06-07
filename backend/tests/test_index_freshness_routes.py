"""
test_index_freshness_routes.py
Route-level coverage for #34C stale repo-index detection.
"""

import subprocess
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.clarification_context import (
    create_clarification_context,
    encode_clarification_context,
)
from backend.projects.project_store import create_project
from backend.repo.index_freshness import (
    compute_working_tree_fingerprint,
    ensure_repo_indexed_and_record,
    get_index_fingerprint_snapshot,
    save_index_fingerprint_snapshot,
)

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
        name=f"Freshness Route Project {uuid.uuid4()}",
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


def _run_git(repo_path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def init_git_repo(repo_path) -> None:
    subprocess.run(
        ["git", "init", str(repo_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    _run_git(repo_path, "config", "user.email", "pipewright-test@example.com")
    _run_git(repo_path, "config", "user.name", "Pipewright Test")
    _run_git(repo_path, "add", ".")
    _run_git(repo_path, "commit", "-m", "initial")


def make_triage(run_id: str, project_id: str, files_expected: list[str]) -> TriageResult:
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
            files_expected=files_expected,
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="Small change.",
        )],
    )


def freshness_model(state: str, reasons: list[str] | None = None) -> dict:
    return {
        "state": state,
        "reasons": reasons or [],
        "current": {
            "branch_name": "main",
            "detached": False,
            "detached_head_label": None,
            "head_sha_short": "aaaaaaaaaaaa",
            "dirty_files_count": 0,
            "git_available": True,
            "is_git_repo": True,
        },
        "indexed": {
            "branch_name": "main",
            "detached": False,
            "detached_head_label": None,
            "head_sha_short": "aaaaaaaaaaaa",
            "dirty_files_count": 0,
            "index_row_count": 1,
            "captured_at": "2026-06-07T00:00:00+00:00",
            "updated_at": "2026-06-07T00:00:00+00:00",
            "snapshot_state": "current",
        },
        "index_row_count": 1,
        "has_index_rows": True,
        "has_snapshot": True,
    }


def install_fake_triage(monkeypatch, files_expected: list[str]):
    async def fake_triage(run_id, project_id, feature_description):
        return make_triage(run_id, project_id, files_expected)

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)


def forbid_run_creation(monkeypatch):
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_triage must not be called")
        ),
    )
    monkeypatch.setattr(
        "backend.routes.chunks.create_chunked_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_chunked_run must not be called")
        ),
    )


def test_current_index_freshness_proceeds_and_surfaces_model(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py"])
    install_fake_triage(monkeypatch, ["app.py"])
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: freshness_model("current"),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["index_freshness"]["state"] == "current"


def test_dirty_only_stale_freshness_proceeds_with_warning(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    init_git_repo(tmp_repo)
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py"])
    clean_fingerprint = compute_working_tree_fingerprint(tmp_repo)
    save_index_fingerprint_snapshot(project["id"], clean_fingerprint, 1)
    (tmp_repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    install_fake_triage(monkeypatch, ["app.py"])
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "stale_index"
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["index_freshness"]["state"] == "stale"
    assert data["index_freshness"]["reasons"] == ["dirty_digest_mismatch"]


@pytest.mark.parametrize(
    "expected_reason",
    [
        "head_sha_mismatch",
        "branch_name_mismatch",
    ],
)
def test_hard_stale_index_returns_no_run_and_reindex_action(
    monkeypatch,
    tmp_repo,
    expected_reason,
):
    project = make_project(tmp_repo)
    stale = freshness_model("stale", [expected_reason])
    forbid_run_creation(monkeypatch)
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: stale,
    )
    monkeypatch.setattr(
        "backend.routes.chunks.get_indexed_paths_and_dirs",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("grounding must not read a stale index")
        ),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update README.md to add install instructions",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "stale_index"
    assert data["outcome"] == "stale_index"
    assert data["run_created"] is False
    assert data["recommended_action"] == "reindex"
    assert data["reindex_endpoint"] == f"/projects/{project['id']}/reindex"
    assert "run_id" not in data
    assert data["index_freshness"] == stale
    assert count_runs(project["id"]) == 0


def test_index_row_count_mismatch_proceeds_with_warning(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    init_git_repo(tmp_repo)
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py", "README.md"])
    fingerprint = compute_working_tree_fingerprint(tmp_repo)
    save_index_fingerprint_snapshot(project["id"], fingerprint, 1)
    install_fake_triage(monkeypatch, ["app.py"])
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data.get("status") != "stale_index"
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["index_freshness"]["state"] == "stale"
    assert data["index_freshness"]["reasons"] == ["index_row_count_mismatch"]


def test_rows_without_snapshot_proceed_with_unknown_freshness(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["README.md", "app.py"])
    install_fake_triage(monkeypatch, ["app.py"])
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["index_freshness"]["state"] == "unknown"
    assert "missing_snapshot" in data["index_freshness"]["reasons"]


def test_cold_start_lazy_index_build_stamps_snapshot(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    init_git_repo(tmp_repo)
    project = make_project(tmp_repo)

    async def fake_triage(run_id, project_id, feature_description):
        ensure_repo_indexed_and_record(project_id, tmp_repo)
        return make_triage(run_id, project_id, ["app.py"])

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["index_freshness"]["state"] == "current"
    assert data["index_freshness"]["has_snapshot"] is True
    assert get_index_fingerprint_snapshot(project["id"]) is not None


def test_unknown_freshness_proceeds_without_crash(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py"])
    install_fake_triage(monkeypatch, ["app.py"])
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: freshness_model("unknown", ["current_fingerprint_unknown"]),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["index_freshness"]["state"] == "unknown"
    assert "current_fingerprint_unknown" in data["index_freshness"]["reasons"]


def test_report_only_does_not_run_freshness_gate(monkeypatch, tmp_repo, tracked_runs):
    project = make_project(tmp_repo)
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("report-only requests must not check freshness")
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
    assert data.get("index_freshness") is None


def test_clarification_selection_reentry_is_stale_gated(monkeypatch, tmp_repo):
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
    forbid_run_creation(monkeypatch)
    monkeypatch.setattr(
        "backend.routes.chunks.get_project_index_freshness",
        lambda *a, **k: freshness_model("stale", ["head_sha_mismatch"]),
    )
    client = TestClient(app)

    response = client.post(
        f"/runs/chunked/clarifications/{token}/select",
        json={"project_id": project["id"], "selection": "1"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "stale_index"
    assert data["run_created"] is False
    assert "run_id" not in data
    assert count_runs(project["id"]) == 0


def test_reindex_then_resubmit_clears_stale_run_creation(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    init_git_repo(tmp_repo)
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py"])
    current = compute_working_tree_fingerprint(tmp_repo)
    stale_snapshot = replace(current, head_sha="0" * 40)
    save_index_fingerprint_snapshot(project["id"], stale_snapshot, 1)
    install_fake_triage(monkeypatch, ["app.py"])
    client = TestClient(app)

    stale_response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert stale_response.status_code == 200
    stale_body = stale_response.json()
    assert stale_body["status"] == "stale_index"
    assert current.head_sha not in str(stale_body)
    assert str(tmp_repo.resolve()) not in str(stale_body)
    assert count_runs(project["id"]) == 0

    reindex_response = client.post(f"/projects/{project['id']}/reindex")
    assert reindex_response.status_code == 200
    assert reindex_response.json()["freshness"]["state"] == "current"

    run_response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update app.py to print hello",
    })

    assert run_response.status_code == 200
    data = run_response.json()
    tracked_runs.append(data["run_id"])
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["index_freshness"]["state"] == "current"


def test_old_index_endpoint_stays_pure_db_and_freshness_endpoint_returns_model(
    monkeypatch,
    tmp_repo,
):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py"])
    monkeypatch.setattr(
        "backend.repo.repo_indexer.build_repo_index",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("GET /index must not rebuild")
        ),
    )
    sentinel = freshness_model("unknown", ["missing_snapshot"])
    called = {"freshness": 0}

    def fake_freshness(project_id, repo_path):
        called["freshness"] += 1
        assert project_id == project["id"]
        assert repo_path == str(tmp_repo)
        return sentinel

    monkeypatch.setattr(
        "backend.routes.projects.get_project_index_freshness",
        fake_freshness,
    )
    client = TestClient(app)

    index_response = client.get(f"/projects/{project['id']}/index")
    freshness_response = client.get(f"/projects/{project['id']}/index/freshness")

    assert index_response.status_code == 200
    assert index_response.json()["files_indexed"] == 1
    assert called["freshness"] == 1
    assert freshness_response.status_code == 200
    assert freshness_response.json() == sentinel
