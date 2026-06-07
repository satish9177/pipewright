"""
test_project_reindex.py
Tests for the #19B backend repo re-index endpoint and index status.

  GET  /projects/{project_id}/index   -> read-only status (no scan)
  POST /projects/{project_id}/reindex -> forced rebuild and freshness snapshot

Re-index is read-only on the repo (no git mutation, no clean-tree requirement)
and lock-aware (409 while a run holds the project repo lock).
"""

import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.pipeline.run_locks import project_repo_lock_sync
from backend.projects.project_store import create_project
from backend.repo.index_freshness import get_index_fingerprint_snapshot

pytestmark = pytest.mark.unit

client = TestClient(app)


def make_project(tmp_repo):
    return create_project(
        name=f"Reindex Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def seed_file_index(project_id: str, paths: list[str]) -> None:
    """Insert index rows directly (indexed_at takes its CURRENT_TIMESTAMP default)."""
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


def indexed_paths(project_id: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT path FROM file_index WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/index
# ---------------------------------------------------------------------------


def test_get_index_unknown_project_returns_404(monkeypatch):
    import backend.repo.repo_indexer as repo_indexer

    called = {"scan": False}
    monkeypatch.setattr(
        repo_indexer,
        "build_repo_index",
        lambda *a, **k: called.__setitem__("scan", True),
    )
    monkeypatch.setattr(
        "backend.routes.projects.reindex_and_record",
        lambda *a, **k: called.__setitem__("scan", True),
    )

    response = client.get("/projects/does-not-exist/index")

    assert response.status_code == 404
    assert called["scan"] is False


def test_get_index_when_no_rows_returns_not_indexed(tmp_repo):
    project = make_project(tmp_repo)

    response = client.get(f"/projects/{project['id']}/index")

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project["id"]
    assert body["files_indexed"] == 0
    assert body["indexed_at"] is None
    assert body["status"] == "not_indexed"


def test_get_index_does_not_scan(tmp_repo, monkeypatch):
    project = make_project(tmp_repo)

    called = {"build": False}
    monkeypatch.setattr(
        "backend.repo.repo_indexer.build_repo_index",
        lambda *a, **k: called.__setitem__("build", True),
    )

    response = client.get(f"/projects/{project['id']}/index")

    assert response.status_code == 200
    assert called["build"] is False


def test_get_index_when_rows_exist_returns_indexed(tmp_repo):
    project = make_project(tmp_repo)
    seed_file_index(project["id"], ["app.py", "README.md", "docs/usage.md"])

    response = client.get(f"/projects/{project['id']}/index")

    assert response.status_code == 200
    body = response.json()
    assert body["files_indexed"] == 3
    assert body["indexed_at"] is not None
    assert body["status"] == "indexed"


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/reindex
# ---------------------------------------------------------------------------


def test_reindex_unknown_project_returns_404(monkeypatch):
    called = {"reindex": False}
    monkeypatch.setattr(
        "backend.routes.projects.reindex_and_record",
        lambda *a, **k: called.__setitem__("reindex", True),
    )

    response = client.post("/projects/does-not-exist/reindex")

    assert response.status_code == 404
    assert called["reindex"] is False


def test_reindex_builds_and_replaces_index(tmp_repo):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    project = make_project(tmp_repo)

    # A stale row for a path that no longer exists on disk.
    seed_file_index(project["id"], ["old/gone.py"])

    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["project_id"] == project["id"]
    assert body["indexed_at"] is not None

    paths = indexed_paths(project["id"])
    assert "old/gone.py" not in paths
    assert "app.py" in paths
    assert "README.md" in paths
    assert body["files_indexed"] == len(paths)
    assert body["files_indexed"] == 2


def test_reindex_adds_newly_created_file(tmp_repo):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    project = make_project(tmp_repo)
    client.post(f"/projects/{project['id']}/reindex")
    assert "README.md" not in indexed_paths(project["id"])

    (tmp_repo / "README.md").write_text("# Hello\n", encoding="utf-8")
    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 200
    assert "README.md" in indexed_paths(project["id"])


def test_reindex_records_freshness_snapshot_for_git_repo(tmp_repo):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    init_git_repo(tmp_repo)
    project = make_project(tmp_repo)

    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 200
    body = response.json()
    assert body["freshness"]["state"] == "current"
    snapshot = get_index_fingerprint_snapshot(project["id"])
    assert snapshot is not None
    assert snapshot.index_row_count == body["files_indexed"]
    assert snapshot.head_sha
    assert snapshot.snapshot_state == "current"


def test_reindex_removes_deleted_file(tmp_repo):
    keep = tmp_repo / "keep.py"
    keep.write_text("x = 1\n", encoding="utf-8")
    drop = tmp_repo / "drop.py"
    drop.write_text("y = 2\n", encoding="utf-8")
    project = make_project(tmp_repo)

    client.post(f"/projects/{project['id']}/reindex")
    assert "drop.py" in indexed_paths(project["id"])

    drop.unlink()
    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 200
    paths = indexed_paths(project["id"])
    assert "drop.py" not in paths
    assert "keep.py" in paths


def test_reindex_excludes_forbidden_binary_and_unsupported(tmp_repo):
    (tmp_repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    # Forbidden secret file.
    (tmp_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    # Unsupported extension.
    (tmp_repo / "data.bin").write_text("not indexed\n", encoding="utf-8")
    # Binary-like content under a supported extension (NUL byte -> skipped).
    (tmp_repo / "image.txt").write_bytes(b"\x00\x00\x00binary")
    project = make_project(tmp_repo)

    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 200
    paths = indexed_paths(project["id"])
    assert "app.py" in paths
    assert ".env" not in paths
    assert "data.bin" not in paths
    assert "image.txt" not in paths


def test_reindex_does_not_require_clean_worktree(tmp_repo, monkeypatch):
    # The route must never gate on git cleanliness. Prove ensure_clean_worktree
    # is not invoked, and that an "uncommitted/untracked" file still indexes.
    import backend.git.local_git as local_git

    calls = {"clean_checks": 0}

    def _fail_if_called(*args, **kwargs):
        calls["clean_checks"] += 1
        raise AssertionError("ensure_clean_worktree must not be called by re-index")

    monkeypatch.setattr(local_git, "ensure_clean_worktree", _fail_if_called)

    (tmp_repo / "uncommitted.py").write_text("z = 3\n", encoding="utf-8")
    project = make_project(tmp_repo)

    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 200
    assert calls["clean_checks"] == 0
    assert "uncommitted.py" in indexed_paths(project["id"])


def test_reindex_returns_409_when_lock_held(tmp_repo, monkeypatch):
    (tmp_repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    project = make_project(tmp_repo)

    called = {"reindex": False}
    monkeypatch.setattr(
        "backend.routes.projects.reindex_and_record",
        lambda *a, **k: called.__setitem__("reindex", True),
    )

    # Hold the project repo lock the way an active run would; the endpoint must
    # refuse with 409 and never rebuild the index.
    with project_repo_lock_sync(project["id"]):
        response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 409
    assert called["reindex"] is False


def test_reindex_error_is_sanitized(tmp_repo, monkeypatch):
    project = make_project(tmp_repo)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom internal traceback secret-ish detail")

    monkeypatch.setattr("backend.routes.projects.reindex_and_record", _boom)

    response = client.post(f"/projects/{project['id']}/reindex")

    assert response.status_code == 400
    detail = response.json()["detail"]
    # Sanitized: the raw internal error text must not be surfaced.
    assert "boom" not in detail
    assert "Traceback" not in detail
