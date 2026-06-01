"""
test_post_commit_reindex.py
PR #19E: post-commit repo index refresh.

After a chunk is committed and marked completed, the orchestrator refreshes the
repo index (best-effort) so a later chunk/run sees files Pipewright just
created/deleted/renamed. These tests verify the refresh runs only on the
committed-success path, after the commit, never on failure/pause, and never
fails the run.

Shared orchestrator test fakes/fixtures are reused from
test_chunked_orchestrator.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.pipeline import chunked_orchestrator
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.patch_applier import PatchApplyOutcome
from backend.pipeline.patch_failures import (
    PATCH_FAILURE_KIND,
    PatchFailureType,
    build_patch_failure_report,
)
from backend.tests.test_chunked_orchestrator import (
    add_code_checkpoint,
    create_run,
    make_coder_result,
    make_planner_result,
    make_test_result,
    patch_git_preflight,
    patch_success_pipeline,
    tracked_runs,  # re-exported fixture
)

pytestmark = pytest.mark.unit


def _indexed_paths(project_id: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT path FROM file_index WHERE project_id = :project_id
        """), {"project_id": project_id}).fetchall()
    return {row[0] for row in rows}


def _seed_index_row(project_id: str, path: str) -> None:
    with engine.begin() as conn:
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


def _spy_reindex(monkeypatch):
    """Record-only spy for build_repo_index (does not scan)."""
    calls = []

    def spy(project_id, repo_path):
        calls.append((project_id, repo_path))
        return {"status": "complete", "project_id": project_id, "files_indexed": 0}

    monkeypatch.setattr(chunked_orchestrator, "build_repo_index", spy)
    return calls


def _commit_only_git(monkeypatch, calls=None):
    """Fake the working-tree/commit so a direct _commit_and_complete_chunk call
    proceeds to commit: tree reads dirty, commit_files is a no-op."""
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "is_working_tree_clean",
        lambda repo_path: False,
    )

    def fake_commit_files(files, message, repo):
        if calls is not None:
            calls.append(('commit', files, message, repo))
        return 'hash'

    monkeypatch.setattr(
        chunked_orchestrator.local_git, "commit_files", fake_commit_files
    )


# ---------------------------------------------------------------------------
# 1. Refresh happens AFTER a successful commit (ordering)
# ---------------------------------------------------------------------------


def test_refresh_runs_after_commit(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = create_run(tmp_repo, tracked_runs)
    chunk = get_chunk_plan_status(run_id).triage.chunks[0]

    calls: list = []
    _commit_only_git(monkeypatch, calls)

    def spy_reindex(project_id, repo_path):
        calls.append(('reindex', project_id, repo_path))

    monkeypatch.setattr(chunked_orchestrator, "build_repo_index", spy_reindex)

    chunked_orchestrator._commit_and_complete_chunk(
        run_id,
        chunk,
        make_coder_result(run_id),
        str(tmp_repo),
        project["id"],
        make_planner_result(run_id),
    )

    kinds = [call[0] for call in calls]
    assert 'commit' in kinds
    assert 'reindex' in kinds
    assert kinds.index('commit') < kinds.index('reindex')


# ---------------------------------------------------------------------------
# 2. A create-file commit refreshes the index so the new file is indexed
# ---------------------------------------------------------------------------


def test_create_file_commit_refreshes_index(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = create_run(tmp_repo, tracked_runs)
    chunk = get_chunk_plan_status(run_id).triage.chunks[0]

    # The file Pipewright "created" is really on disk; the real build_repo_index
    # (NOT mocked here) must pick it up after the commit.
    (tmp_repo / "README.md").write_text("# Title\n", encoding="utf-8")
    assert "README.md" not in _indexed_paths(project["id"])

    _commit_only_git(monkeypatch)

    chunked_orchestrator._commit_and_complete_chunk(
        run_id,
        chunk,
        make_coder_result(run_id),
        str(tmp_repo),
        project["id"],
        make_planner_result(run_id),
    )

    assert "README.md" in _indexed_paths(project["id"])


# ---------------------------------------------------------------------------
# 3. A commit refresh drops index rows for files no longer on disk
# ---------------------------------------------------------------------------


def test_delete_file_commit_removes_stale_index_row(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    chunk = get_chunk_plan_status(run_id).triage.chunks[0]

    (tmp_repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    # A stale row for a file that is not on disk anymore.
    _seed_index_row(project["id"], "deleted.py")
    assert "deleted.py" in _indexed_paths(project["id"])

    _commit_only_git(monkeypatch)

    chunked_orchestrator._commit_and_complete_chunk(
        run_id,
        chunk,
        make_coder_result(run_id),
        str(tmp_repo),
        project["id"],
        make_planner_result(run_id),
    )

    paths = _indexed_paths(project["id"])
    assert "deleted.py" not in paths
    assert "keep.py" in paths


# ---------------------------------------------------------------------------
# 4. A refresh failure never fails the chunk/run (best-effort, logged)
# ---------------------------------------------------------------------------


def test_refresh_failure_does_not_fail_chunk_or_run(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    chunk = get_chunk_plan_status(run_id).triage.chunks[0]

    _commit_only_git(monkeypatch)

    def boom(project_id, repo_path):
        raise RuntimeError("index boom")

    monkeypatch.setattr(chunked_orchestrator, "build_repo_index", boom)

    # Capture the warning directly off the module logger (robust to the
    # project's log-propagation config).
    warnings: list = []
    monkeypatch.setattr(
        chunked_orchestrator.logger,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )

    # Must NOT raise.
    chunked_orchestrator._commit_and_complete_chunk(
        run_id,
        chunk,
        make_coder_result(run_id),
        str(tmp_repo),
        project["id"],
        make_planner_result(run_id),
    )

    status = get_chunk_plan_status(run_id)
    assert status.chunks[0].status == "completed"
    with engine.connect() as conn:
        run_status = conn.execute(text("""
            SELECT status FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()[0]
    assert run_status != "failed"
    assert any(
        args and "post-commit index refresh failed" in str(args[0])
        for args in warnings
    )


# ---------------------------------------------------------------------------
# 5. No refresh on a patch (apply) failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_refresh_on_patch_failure(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        allowed_files=["created_1.py"],
        chunk_number=1,
        failed_step="patch",
    )

    def fail_apply(code, run_id, chunk_number=0, *, files_expected, repo_path=None):
        return PatchApplyOutcome.from_failure(report)

    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", fail_apply)
    reindex_calls = _spy_reindex(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert reindex_calls == []


# ---------------------------------------------------------------------------
# 6. No refresh on a test failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_refresh_on_test_failure(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)

    def failed_tests(patch, run_id, chunk_number=0):
        return make_test_result(run_id, False)

    monkeypatch.setattr(chunked_orchestrator, "run_tests", failed_tests)
    reindex_calls = _spy_reindex(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "failed"
    assert reindex_calls == []


# ---------------------------------------------------------------------------
# 7. No refresh while awaiting approval; one refresh after the approval commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_refresh_before_approval_then_one_after(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(
        tmp_repo, tracked_runs, chunks=1, review_chunks={1}
    )
    patch_git_preflight(monkeypatch)
    patch_success_pipeline(monkeypatch, run_id)
    reindex_calls = _spy_reindex(monkeypatch)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)
    assert result["status"] == "awaiting_chunk_approval"
    # High-risk chunk paused before commit: nothing committed, no refresh.
    assert reindex_calls == []

    add_code_checkpoint(run_id, 1)
    approve = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)

    assert approve["status"] == "chunk_approved"
    # The human-approved commit went through _commit_and_complete_chunk once.
    assert len(reindex_calls) == 1


# ---------------------------------------------------------------------------
# 8. Success completion_summary is unchanged by the refresh
# ---------------------------------------------------------------------------


def test_completion_summary_unchanged_by_refresh(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    chunk = get_chunk_plan_status(run_id).triage.chunks[0]

    _commit_only_git(monkeypatch)
    _spy_reindex(monkeypatch)

    chunked_orchestrator._commit_and_complete_chunk(
        run_id,
        chunk,
        make_coder_result(run_id),
        str(tmp_repo),
        project["id"],
        make_planner_result(run_id),
    )

    status = get_chunk_plan_status(run_id)
    completed = status.chunks[0]
    assert completed.status == "completed"
    # A normal success summary, not a patch-failure payload.
    summary = completed.completion_summary or ""
    assert PATCH_FAILURE_KIND not in summary
    assert "created_1.py" in summary
    assert "deleted_1.py" in summary
