"""
test_index_freshness.py
Focused tests for #34B working-tree/index freshness fingerprints.
"""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.db import database
from backend.db.database import engine
from backend.repo.index_freshness import (
    DETACHED_HEAD_PREFIX,
    IndexFreshnessState,
    StoredIndexFingerprint,
    WorkingTreeFingerprint,
    compare_index_freshness,
    compute_working_tree_fingerprint,
    dirty_digest_from_status,
    get_index_fingerprint_snapshot,
    save_index_fingerprint_snapshot,
)

pytestmark = pytest.mark.unit


def _known_fingerprint(**overrides) -> WorkingTreeFingerprint:
    dirty_digest, dirty_count = dirty_digest_from_status("")
    values = {
        "repo_path_resolved": "C:/repo",
        "is_git_repo": True,
        "git_available": True,
        "branch_name": "feature/demo",
        "branch_is_detached": False,
        "detached_head_label": None,
        "head_sha": "a" * 40,
        "dirty_digest": dirty_digest,
        "dirty_files_count": dirty_count,
        "captured_at": "2026-06-07T00:00:00+00:00",
        "git_error": None,
    }
    values.update(overrides)
    return WorkingTreeFingerprint(**values)


def _stored_snapshot(**overrides) -> StoredIndexFingerprint:
    fp = _known_fingerprint()
    values = {
        "project_id": "project-1",
        "repo_path_resolved": fp.repo_path_resolved,
        "branch_name": fp.branch_name,
        "branch_is_detached": fp.branch_is_detached,
        "detached_head_label": fp.detached_head_label,
        "head_sha": fp.head_sha,
        "dirty_digest": fp.dirty_digest,
        "dirty_files_count": fp.dirty_files_count,
        "index_row_count": 2,
        "captured_at": fp.captured_at,
        "updated_at": "2026-06-07T00:00:01+00:00",
    }
    values.update(overrides)
    return StoredIndexFingerprint(**values)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", str(repo)], capture_output=True, text=True, check=True)
    _run_git(repo, "config", "user.email", "pipewright-test@example.com")
    _run_git(repo, "config", "user.name", "Pipewright Test")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial")


def _insert_project(project_id: str, repo_path: Path) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO projects (id, name, repo_path, test_command)
            VALUES (:id, :name, :repo_path, 'python --version')
        """), {
            "id": project_id,
            "name": f"Index Freshness {project_id}",
            "repo_path": str(repo_path),
        })


def test_compare_current_when_identity_matches():
    comparison = compare_index_freshness(
        _known_fingerprint(),
        _stored_snapshot(),
        current_index_row_count=2,
    )

    assert comparison.state is IndexFreshnessState.CURRENT
    assert comparison.reasons == ()


def test_compare_missing_snapshot():
    comparison = compare_index_freshness(_known_fingerprint(), None)

    assert comparison.state is IndexFreshnessState.MISSING
    assert comparison.reasons == ("missing_snapshot",)


def test_compare_unknown_current():
    unknown = _known_fingerprint(
        is_git_repo=False,
        head_sha=None,
        dirty_digest=None,
        git_error="not a git repository",
    )

    comparison = compare_index_freshness(unknown, _stored_snapshot())

    assert comparison.state is IndexFreshnessState.UNKNOWN
    assert comparison.reasons == ("current_fingerprint_unknown",)


@pytest.mark.parametrize(
    ("current_overrides", "stored_overrides", "reason"),
    [
        ({"branch_name": "feature/other"}, {}, "branch_name_mismatch"),
        ({"head_sha": "b" * 40}, {}, "head_sha_mismatch"),
        ({"dirty_digest": "c" * 64}, {}, "dirty_digest_mismatch"),
        ({"repo_path_resolved": "C:/other"}, {}, "repo_path_mismatch"),
        (
            {
                "branch_name": None,
                "branch_is_detached": True,
                "detached_head_label": f"{DETACHED_HEAD_PREFIX}{'a' * 12}",
            },
            {},
            "branch_detached_state_mismatch",
        ),
        ({}, {"index_row_count": 3}, "index_row_count_mismatch"),
    ],
)
def test_compare_stale_reasons(current_overrides, stored_overrides, reason):
    comparison = compare_index_freshness(
        _known_fingerprint(**current_overrides),
        _stored_snapshot(**stored_overrides),
        current_index_row_count=2,
    )

    assert comparison.state is IndexFreshnessState.STALE
    assert reason in comparison.reasons


def test_dirty_digest_normalization_is_deterministic():
    digest_a, count_a = dirty_digest_from_status(" M b.py\r\n?? a.py\r\n")
    digest_b, count_b = dirty_digest_from_status("?? a.py\n M b.py\n")

    assert digest_a == digest_b
    assert count_a == 2
    assert count_b == 2


def test_non_git_repo_returns_unknown_not_crash(tmp_path):
    repo = tmp_path / "not-a-git-repo"
    repo.mkdir()

    fingerprint = compute_working_tree_fingerprint(repo)

    assert fingerprint.is_known is False
    assert fingerprint.is_git_repo is False
    assert fingerprint.git_error is not None
    comparison = compare_index_freshness(fingerprint, _stored_snapshot())
    assert comparison.state is IndexFreshnessState.UNKNOWN


def test_detached_head_representation(tmp_repo):
    _init_repo(tmp_repo)
    _run_git(tmp_repo, "checkout", "--detach", "HEAD")

    fingerprint = compute_working_tree_fingerprint(tmp_repo)

    assert fingerprint.is_known is True
    assert fingerprint.branch_name is None
    assert fingerprint.branch_is_detached is True
    assert fingerprint.head_sha is not None
    assert fingerprint.detached_head_label == (
        f"{DETACHED_HEAD_PREFIX}{fingerprint.head_sha[:12]}"
    )


def test_raw_git_status_output_not_exposed_in_fingerprint_repr(tmp_repo):
    _init_repo(tmp_repo)
    secretish_name = "secret-token-value.txt"
    (tmp_repo / secretish_name).write_text("do not expose path\n", encoding="utf-8")

    fingerprint = compute_working_tree_fingerprint(tmp_repo)

    assert fingerprint.is_known is True
    assert fingerprint.dirty_files_count == 1
    assert secretish_name not in repr(fingerprint)
    assert secretish_name not in str(fingerprint.__dict__)


def test_persistence_save_get_update(tmp_repo):
    project_id = f"index-freshness-{uuid.uuid4()}"
    _insert_project(project_id, tmp_repo)
    first = _known_fingerprint(repo_path_resolved=str(tmp_repo.resolve()))

    saved = save_index_fingerprint_snapshot(project_id, first, 2)
    loaded = get_index_fingerprint_snapshot(project_id)

    assert loaded == saved
    assert loaded is not None
    assert loaded.project_id == project_id
    assert loaded.index_row_count == 2
    assert loaded.head_sha == first.head_sha

    second = _known_fingerprint(
        repo_path_resolved=str(tmp_repo.resolve()),
        branch_name="feature/updated",
        head_sha="d" * 40,
    )
    updated = save_index_fingerprint_snapshot(project_id, second, 7)

    assert updated.project_id == project_id
    assert updated.branch_name == "feature/updated"
    assert updated.head_sha == "d" * 40
    assert updated.index_row_count == 7


def test_real_compute_save_load_compare_round_trip_is_current(tmp_repo):
    _init_repo(tmp_repo)
    project_id = f"index-freshness-roundtrip-{uuid.uuid4()}"
    _insert_project(project_id, tmp_repo)
    index_row_count = 1

    fingerprint = compute_working_tree_fingerprint(tmp_repo)
    assert fingerprint.is_known is True

    saved = save_index_fingerprint_snapshot(
        project_id,
        fingerprint,
        index_row_count,
    )
    loaded = get_index_fingerprint_snapshot(project_id)
    comparison = compare_index_freshness(
        current=fingerprint,
        stored=loaded,
        current_index_row_count=index_row_count,
    )

    assert loaded == saved
    assert loaded is not None
    assert comparison.state is IndexFreshnessState.CURRENT
    assert loaded.repo_path_resolved == fingerprint.repo_path_resolved
    assert loaded.branch_name == fingerprint.branch_name
    assert loaded.branch_is_detached is fingerprint.branch_is_detached
    assert isinstance(loaded.branch_is_detached, bool)
    assert loaded.detached_head_label == fingerprint.detached_head_label
    assert loaded.head_sha == fingerprint.head_sha
    assert loaded.dirty_digest == fingerprint.dirty_digest
    assert loaded.dirty_files_count == fingerprint.dirty_files_count
    assert loaded.index_row_count == index_row_count


def test_save_snapshot_rejects_unknown_fingerprint():
    unknown = _known_fingerprint(
        is_git_repo=False,
        head_sha=None,
        dirty_digest=None,
        git_error="not a git repository",
    )

    with pytest.raises(RuntimeError, match="cannot persist unknown"):
        save_index_fingerprint_snapshot("project-unknown", unknown, 1)


def test_save_snapshot_rejects_empty_project_id():
    with pytest.raises(RuntimeError, match="project_id is required"):
        save_index_fingerprint_snapshot("", _known_fingerprint(), 1)


def test_save_snapshot_rejects_negative_index_row_count():
    with pytest.raises(RuntimeError, match="index_row_count must be >= 0"):
        save_index_fingerprint_snapshot("project-negative", _known_fingerprint(), -1)


def test_idempotent_schema_creation_migration():
    temp_engine = create_engine("sqlite:///:memory:")
    with temp_engine.connect() as conn:
        database._migrate_db(conn)
        database._migrate_db(conn)
        conn.commit()

        table = conn.execute(text("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'project_index_fingerprints'
        """)).fetchone()
        columns = conn.execute(text("""
            PRAGMA table_info(project_index_fingerprints)
        """)).fetchall()
        indexes = conn.execute(text("""
            SELECT name FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_project_index_fingerprints_updated'
        """)).fetchone()

    column_names = {row._mapping["name"] for row in columns}
    assert table is not None
    assert "project_id" in column_names
    assert "repo_path_resolved" in column_names
    assert "head_sha" in column_names
    assert "dirty_digest" in column_names
    assert "index_row_count" in column_names
    assert indexes is not None
