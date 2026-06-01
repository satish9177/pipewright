"""
test_chunk_routes_stale_reindex.py
PR #19C: stale explicit-path auto re-index and retry once.

When the explicit-target resolver returns NOT_FOUND but the named file actually
exists on disk under the project repo, the chunked-run route re-indexes ONCE and
re-resolves. These tests cover that recovery and its safety boundaries: missing
on disk, hard cap, vague requests, forbidden targets, excluded files, ambiguity
after re-index, case mismatch, and the active-repo-lock conflict.

Shared route-test helpers are reused from test_chunk_routes.
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.pipeline.run_locks import project_repo_lock_sync
from backend.tests.test_chunk_routes import (
    _assert_no_runs,
    _forbid_triage,
    make_project,
    make_triage_with_files,
    seed_file_index,
    tracked_runs,  # re-export the fixture
)

pytestmark = pytest.mark.unit


def _spy_build_repo_index(monkeypatch, *, passthrough: bool):
    """Spy on the re-index call made from the chunked-run route.

    passthrough=True runs the real build_repo_index (so the index actually
    refreshes); passthrough=False makes it a no-op (simulates a scan that did
    not pick up the target). Returns the call-args list.
    """
    import backend.routes.chunks as chunks_routes

    calls = []
    real = chunks_routes.build_repo_index

    def spy(project_id, repo_path):
        calls.append((project_id, repo_path))
        if passthrough:
            return real(project_id, repo_path)
        return {"status": "complete", "project_id": project_id, "files_indexed": 0}

    monkeypatch.setattr(chunks_routes, "build_repo_index", spy)
    return calls


def test_stale_explicit_target_on_disk_reindexes_once_and_grounds(
    monkeypatch, tmp_repo, tracked_runs
):
    # A: README.md is on disk but the index is stale (missing it).
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)

    async def fake_triage(run_id, project_id, feature_description):
        return make_triage_with_files(
            run_id, project_id, files_expected=["backend/app.py"],
            title="Docs edit", description="Edit docs.",
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello to README.md",
    })

    assert response.status_code == 200
    data = response.json()
    tracked_runs.append(data["run_id"])
    # Re-indexed exactly once, then proceeded to a normal chunk plan.
    assert len(calls) == 1
    assert data.get("status") != "needs_clarification"
    assert data["chunk_plan_status"] == "awaiting_approval"
    assert data["chunks"][0]["files_expected"] == ["README.md"]


def test_stale_recovery_missing_on_disk_keeps_clarification(monkeypatch, tmp_repo):
    # B: README.md is neither indexed nor on disk -> no re-index, same behavior.
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello to README.md",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "README" in data["message"]
    assert calls == []
    _assert_no_runs(project["id"])


def test_stale_recovery_reindexes_at_most_once(monkeypatch, tmp_repo):
    # C: on disk, but the (mocked) re-index does not pick it up -> safe
    # clarification, build called exactly once, no loop.
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=False)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello to README.md",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert len(calls) == 1
    _assert_no_runs(project["id"])


def test_vague_request_does_not_auto_reindex(monkeypatch, tmp_repo):
    # D: vague request -> no explicit target -> no re-index, even if a file
    # exists on disk but not in the index.
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)
    monkeypatch.setattr(
        "backend.routes.chunks.run_triage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_triage must not be called for vague requests")
        ),
    )
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "implement a small safe change",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["intent"] == "implementation"
    assert calls == []
    _assert_no_runs(project["id"])


def test_forbidden_target_does_not_auto_reindex(monkeypatch, tmp_repo):
    # E: forbidden target -> safe refusal, never re-indexed.
    (tmp_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello to .env",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "protected" in data["message"]
    assert calls == []
    _assert_no_runs(project["id"])


def test_unsupported_or_excluded_file_on_disk_returns_excluded_clarification(
    monkeypatch, tmp_repo
):
    # F: a .md file on disk that the indexer excludes (binary content) -> after
    # one re-index it is still not indexed -> "exists but excluded" clarification,
    # NOT "create it first".
    (tmp_repo / "weird.md").write_bytes(b"\x00\x00hello")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello to weird.md",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert "exists on disk" in data["message"]
    assert "unsupported or excluded" in data["message"]
    assert "create" not in data["message"].lower()
    assert len(calls) == 1
    _assert_no_runs(project["id"])


def test_ambiguous_after_reindex_returns_candidate_clarification(
    monkeypatch, tmp_repo
):
    # G: two READMEs on disk, none indexed -> after re-index the alias resolves
    # to AMBIGUOUS -> ranked candidate clarification with a clarification_id.
    (tmp_repo / "README.md").write_text("# Root\n", encoding="utf-8")
    (tmp_repo / "docs").mkdir()
    (tmp_repo / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello in the readme",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert set(data["candidates"]) == {"README.md", "docs/README.md"}
    assert "clarification_id" in data
    assert len(calls) == 1
    _assert_no_runs(project["id"])


def test_case_mismatch_does_not_silently_rewrite(monkeypatch, tmp_repo):
    # H: disk has README.md; user asks for readme.md. We must never silently
    # edit/create the wrong-cased path. On a case-insensitive FS we surface the
    # real path as a candidate; on a case-sensitive FS we keep the not-found
    # clarification. Either way: a clarification, never a run.
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    _spy_build_repo_index(monkeypatch, passthrough=True)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "add hello to readme.md",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    # If a case-insensitive candidate was surfaced, it must be the real casing.
    if data.get("candidates"):
        assert "README.md" in data["candidates"]
    _assert_no_runs(project["id"])


def test_stale_recovery_returns_409_when_repo_lock_held(monkeypatch, tmp_repo):
    # I: a run is actively holding the project repo lock -> the stale re-index
    # must not race it. Expect 409 and no build_repo_index call.
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["backend/app.py"])

    calls = _spy_build_repo_index(monkeypatch, passthrough=True)
    _forbid_triage(monkeypatch)
    client = TestClient(app)

    with project_repo_lock_sync(project["id"]):
        response = client.post("/runs/chunked", json={
            "project_id": project["id"],
            "feature_description": "add hello to README.md",
        })

    assert response.status_code == 409
    assert calls == []
    _assert_no_runs(project["id"])
