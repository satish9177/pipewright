"""
test_interruption_guidance.py
Tests for #32E: git-aware interruption guidance.

Detection is strictly read-only. These tests prove the detector classifies
clean/dirty/missing/non-git repos, never mutates the repo, and that startup
recovery surfaces human-gated guidance for interrupted runs WITHOUT performing
any Git mutation and WITHOUT changing existing DB-state reconciliation.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.git import local_git
from backend.git.local_git import detect_uncommitted_changes, run_git
from backend.runtime import startup_recovery
from backend.runtime.startup_recovery import (
    INTERRUPTED_DIRTY_TREE_MESSAGE,
    RESTART_RECOVERY_MESSAGE,
    recover_interrupted_runs,
)

pytestmark = pytest.mark.unit

# Git subcommands that mutate a repo. Detection must never use any of these.
_FORBIDDEN_GIT_SUBCOMMANDS = {
    "reset", "stash", "checkout", "clean", "commit",
    "restore", "add", "rm", "merge", "rebase", "push", "switch",
}


def _init_git_repo(path) -> None:
    result = run_git(["init"], str(path))
    if result.returncode != 0:
        pytest.skip(f"git not available or init failed: {result.stderr.strip()}")


def _make_dirty(path) -> None:
    # An untracked file makes `git status --porcelain` non-empty without needing
    # a commit (which would require user.name/email config).
    (path / "uncommitted.txt").write_text("work in progress\n", encoding="utf-8")


@pytest.fixture()
def tracked():
    project_ids: list[str] = []
    run_ids: list[str] = []
    yield project_ids, run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM chunks WHERE run_id = :id"), {"id": run_id})
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :id"), {"id": run_id}
            )
        for project_id in project_ids:
            conn.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": project_id}
            )


def _insert_interrupted_run(tracked, repo_path: str) -> tuple[str, str]:
    project_ids, run_ids = tracked
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    project_ids.append(project_id)
    run_ids.append(run_id)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO projects (id, name, repo_path, test_command, status)
            VALUES (:pid, 'Interruption Project', :repo_path, 'python --version', 'active')
        """), {"pid": project_id, "repo_path": repo_path})
        # Inserted as running_chunks so recovery flips it to interrupted,
        # exercising the real reconciliation path.
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step)
            VALUES (:rid, :pid, 'Interrupted run', 'running_chunks', 'chunk_1')
        """), {"rid": run_id, "pid": project_id})
    return run_id, project_id


def _entry_for(result: dict, run_id: str) -> dict | None:
    for entry in result.get("dirty_tree_guidance", []):
        if entry["run_id"] == run_id:
            return entry
    return None


# --- read-only detector --------------------------------------------------

def test_detect_dirty_repo(tmp_repo):
    _init_git_repo(tmp_repo)
    _make_dirty(tmp_repo)

    status = detect_uncommitted_changes(str(tmp_repo))

    assert status.state == "dirty"
    assert status.is_dirty is True
    assert "uncommitted.txt" in status.dirty_files


def test_detect_clean_repo(tmp_repo):
    _init_git_repo(tmp_repo)

    status = detect_uncommitted_changes(str(tmp_repo))

    assert status.state == "clean"
    assert status.is_dirty is False
    assert status.dirty_files == ()


def test_detect_missing_path_is_safe():
    status = detect_uncommitted_changes("/no/such/path/xyz123")
    assert status.state == "missing_path"
    assert status.is_dirty is False


def test_detect_non_git_dir_is_safe(tmp_path):
    # tmp_path is pytest's system-temp dir, outside any git repo. (The repo's own
    # tmp_repo fixture lives inside the Pipewright git tree, so git would resolve
    # it to the parent repo — tmp_path avoids that.)
    status = detect_uncommitted_changes(str(tmp_path))
    assert status.state == "not_a_repo"
    assert status.is_dirty is False


def test_detect_empty_path_is_safe():
    assert detect_uncommitted_changes("").state == "missing_path"
    assert detect_uncommitted_changes("   ").state == "missing_path"


def test_detection_does_not_mutate_repo(tmp_repo):
    _init_git_repo(tmp_repo)
    _make_dirty(tmp_repo)

    before = run_git(["status", "--porcelain", "-uall"], str(tmp_repo)).stdout
    status = detect_uncommitted_changes(str(tmp_repo))
    after = run_git(["status", "--porcelain", "-uall"], str(tmp_repo)).stdout

    assert status.is_dirty
    # The working tree is byte-for-byte identical before and after detection.
    assert before == after
    assert (tmp_repo / "uncommitted.txt").read_text(encoding="utf-8") == (
        "work in progress\n"
    )


# --- startup recovery integration ----------------------------------------

def test_interrupted_run_with_dirty_repo_surfaces_guidance(tmp_repo, tracked):
    _init_git_repo(tmp_repo)
    _make_dirty(tmp_repo)
    run_id, _project_id = _insert_interrupted_run(tracked, str(tmp_repo))

    result = recover_interrupted_runs()

    entry = _entry_for(result, run_id)
    assert entry is not None
    assert entry["state"] == "dirty"
    assert "uncommitted.txt" in entry["dirty_files"]
    assert entry["guidance"] == INTERRUPTED_DIRTY_TREE_MESSAGE


def test_interrupted_run_with_clean_repo_has_no_dirty_guidance(tmp_repo, tracked):
    _init_git_repo(tmp_repo)
    run_id, _project_id = _insert_interrupted_run(tracked, str(tmp_repo))

    result = recover_interrupted_runs()

    assert _entry_for(result, run_id) is None


def test_interrupted_run_with_invalid_repo_path_is_safe(tracked):
    bad_path = "/no/such/repo/path/zzz999"
    run_id, _project_id = _insert_interrupted_run(tracked, bad_path)

    # Must not raise; surfaces a safe warning entry instead.
    result = recover_interrupted_runs()

    entry = _entry_for(result, run_id)
    assert entry is not None
    assert entry["state"] in {"missing_path", "not_a_repo", "error"}
    assert entry["dirty_files"] == []


def test_existing_db_state_reconciliation_is_unchanged(tmp_repo, tracked):
    # DB-state behavior from the original recovery must be preserved: a
    # running_chunks run becomes interrupted and a running chunk resets.
    _init_git_repo(tmp_repo)
    run_id, project_id = _insert_interrupted_run(tracked, str(tmp_repo))
    chunk_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO chunks
            (id, run_id, project_id, chunk_number, title, description, status, started_at)
            VALUES (:cid, :rid, :pid, 1, 'Chunk', 'Running', 'running', :started)
        """), {
            "cid": chunk_id,
            "rid": run_id,
            "pid": project_id,
            "started": "2026-05-25T00:00:00+00:00",
        })

    result = recover_interrupted_runs()

    assert result["chunks_reset"] >= 1
    assert result["runs_interrupted"] >= 1
    with engine.connect() as conn:
        chunk = conn.execute(text(
            "SELECT status, started_at, error_message FROM chunks WHERE id = :id"
        ), {"id": chunk_id}).fetchone()
        run = conn.execute(text(
            "SELECT status, current_step FROM pipeline_runs WHERE id = :id"
        ), {"id": run_id}).fetchone()
    assert chunk[0] == "pending"
    assert chunk[1] is None
    assert chunk[2] == RESTART_RECOVERY_MESSAGE
    assert run[0] == "interrupted"
    assert run[1] == "interrupted"


def test_recovery_never_runs_mutating_git_commands(tmp_repo, tracked, monkeypatch):
    _init_git_repo(tmp_repo)
    _make_dirty(tmp_repo)
    _insert_interrupted_run(tracked, str(tmp_repo))

    recorded: list[list[str]] = []
    real_run_git = local_git.run_git

    def spy(args, repo_path, timeout=30):
        recorded.append(list(args))
        return real_run_git(args, repo_path, timeout)

    monkeypatch.setattr(local_git, "run_git", spy)

    recover_interrupted_runs()

    # Detection ran (a git repo was inspected) and used ONLY read-only commands.
    assert recorded, "expected detection to inspect the interrupted run's repo"
    for args in recorded:
        assert args, "empty git args recorded"
        assert args[0] not in _FORBIDDEN_GIT_SUBCOMMANDS
        assert args[0] in {"rev-parse", "status"}
