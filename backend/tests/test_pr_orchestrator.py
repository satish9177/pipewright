"""
test_pr_orchestrator.py
Tests for Phase 2B-4D final push and PR creation.
No real push. No real GitHub calls.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import pr_orchestrator
from backend.pipeline.chunk_store import create_chunked_run, save_chunk_completion_summary
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


class FakePull:
    def __init__(self, number: int = 123, url: str = "https://github.com/acme/demo/pull/123"):
        self.number = number
        self.html_url = url


class FakeRepo:
    def __init__(self, existing=None, fail_create: bool = False):
        self.existing = existing or []
        self.fail_create = fail_create
        self.created = []

    def get_pulls(self, state, head, base):
        self.last_get_pulls = {"state": state, "head": head, "base": base}
        return self.existing

    def create_pull(self, title, body, head, base):
        if self.fail_create:
            raise RuntimeError("GitHub create failed")
        self.created.append({"title": title, "body": body, "head": head, "base": base})
        return FakePull(456, "https://github.com/acme/demo/pull/456")


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


def make_project(tmp_repo, owner="acme", repo="demo"):
    return create_project(
        name=f"PR Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
        github_token="ghp_test",
        github_owner=owner,
        github_repo=repo,
        github_base_branch="main",
    )


def make_triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Push final PR",
        complexity="medium",
        total_chunks=2,
        reasoning="Two chunks.",
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="First chunk",
                description="Do first chunk",
                files_expected=["src/one.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="first",
            ),
            ChunkDefinition(
                chunk_number=2,
                title="Second chunk",
                description="Do second chunk",
                files_expected=["src/two.py"],
                depends_on=[1],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="second",
            ),
        ],
    )


def create_final_approved_run(tmp_repo, tracked_runs, status="final_approved"):
    project = make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Push final PR",
        make_triage(run_id, project["id"]),
    )
    long_summary = "x" * 700
    save_chunk_completion_summary(run_id, 1, {"summary": "chunk one done"})
    save_chunk_completion_summary(run_id, 2, {"summary": long_summary})
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE chunks
            SET status = 'completed'
            WHERE run_id = :run_id
        """), {"run_id": run_id})
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                chunk_plan_status = 'approved',
                total_chunks = 2
            WHERE id = :run_id
        """), {"run_id": run_id, "status": status})
    return run_id, project


def patch_git(monkeypatch, calls, remote_exists=False, current_branch=None, dirty=False):
    monkeypatch.setattr(pr_orchestrator.local_git, "ensure_git_repo", lambda repo: calls.append(("ensure", repo)))
    monkeypatch.setattr(pr_orchestrator.local_git, "branch_exists", lambda branch, repo: True)
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "get_current_branch",
        lambda repo: current_branch or calls[-1][1] if calls and calls[-1][0] == "expected_branch" else "pipewright/unused",
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: (_ for _ in ()).throw(RuntimeError("[GIT] dirty")) if dirty else None,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "get_remote_url",
        lambda repo: "https://github.com/acme/demo.git",
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "branch_exists_remote",
        lambda repo, branch: calls.append(("remote_exists", branch)) or remote_exists,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "push_branch",
        lambda branch, repo: calls.append(("push", branch, repo)),
    )

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "run_git",
        lambda args, repo: calls.append(("run_git", args, repo)) or Result(),
    )


def patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=False, dirty=False):
    patch_git(
        monkeypatch,
        calls,
        remote_exists=remote_exists,
        current_branch=branch_name,
        dirty=dirty,
    )


def patch_github(monkeypatch, repo):
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda token, owner, repo_name: repo,
    )


def test_push_pr_rejects_run_not_final_approved_or_push_failed(tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs, status="running")

    with pytest.raises(RuntimeError, match="not ready"):
        pr_orchestrator.push_and_create_pr(run_id)


def test_push_pr_rejects_final_rejected(tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs, status="final_rejected")

    with pytest.raises(RuntimeError, match="not ready"):
        pr_orchestrator.push_and_create_pr(run_id)


def test_push_pr_returns_existing_db_pr_url_idempotently(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs, status="complete")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET pr_url = 'https://github.com/acme/demo/pull/9',
                pr_number = 9,
                branch_name = :branch_name
            WHERE id = :run_id
        """), {"run_id": run_id, "branch_name": f"pipewright/{run_id[:8]}"})

    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "push_branch",
        lambda *args: (_ for _ in ()).throw(AssertionError("push not expected")),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert result["pr_number"] == 9
    assert result["pr_url"].endswith("/9")


def test_push_pr_verifies_expected_branch_and_pushes_when_remote_missing(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=False)
    fake_repo = FakeRepo()
    patch_github(monkeypatch, fake_repo)

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert ("push", branch_name, str(tmp_repo)) in calls
    assert fake_repo.created[0]["head"] == branch_name


def test_push_pr_rejects_dirty_worktree(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, dirty=True)
    patch_github(monkeypatch, FakeRepo())

    with pytest.raises(RuntimeError, match="dirty"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert not any(call[0] == "push" for call in calls)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, push_error FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "push_failed"
    assert "dirty" in row[1]


def test_push_pr_skips_push_when_remote_branch_exists(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    patch_github(monkeypatch, FakeRepo())

    pr_orchestrator.push_and_create_pr(run_id)

    assert not any(call[0] == "push" for call in calls)


def test_push_pr_checks_existing_github_pr_before_creating(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    existing = FakePull(77, "https://github.com/acme/demo/pull/77")
    fake_repo = FakeRepo(existing=[existing])
    patch_github(monkeypatch, fake_repo)

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["pr_number"] == 77
    assert fake_repo.created == []
    assert fake_repo.last_get_pulls == {
        "state": "open",
        "head": f"acme:{branch_name}",
        "base": "main",
    }
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, pr_url, pr_number FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "complete"
    assert row[1].endswith("/77")
    assert row[2] == 77


def test_build_pr_body_includes_details_and_truncates_summaries(tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)

    body = pr_orchestrator.build_pr_body(run_id)

    assert "Push final PR" in body
    assert run_id in body
    assert f"pipewright/{run_id[:8]}" in body
    assert "First chunk - completed" in body
    assert "Created by Pipewright." in body
    assert "[truncated]" in body
    assert len(body) < 10000


def test_push_succeeds_pr_create_fails_then_retry_skips_push_and_creates(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    remote_state = {"exists": False}
    patch_git_for_branch(monkeypatch, calls, branch_name)
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "branch_exists_remote",
        lambda repo, branch: remote_state["exists"],
    )

    def fake_push(branch, repo):
        calls.append(("push", branch, repo))
        remote_state["exists"] = True

    monkeypatch.setattr(pr_orchestrator.local_git, "push_branch", fake_push)
    patch_github(monkeypatch, FakeRepo(fail_create=True))
    with pytest.raises(RuntimeError, match="GitHub create failed"):
        pr_orchestrator.push_and_create_pr(run_id)

    patch_github(monkeypatch, FakeRepo())
    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert len([call for call in calls if call[0] == "push"]) == 1


def test_pr_created_but_db_save_missing_retry_finds_existing_pr(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    original_save = pr_orchestrator._save_pr_metadata
    monkeypatch.setattr(
        pr_orchestrator,
        "_save_pr_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db failed")),
    )
    patch_github(monkeypatch, FakeRepo())
    with pytest.raises(RuntimeError, match="db failed"):
        pr_orchestrator.push_and_create_pr(run_id)

    monkeypatch.setattr(pr_orchestrator, "_save_pr_metadata", original_save)
    patch_github(monkeypatch, FakeRepo(existing=[FakePull(88, "https://github.com/acme/demo/pull/88")]))
    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["pr_number"] == 88


def test_github_auth_failure_sets_push_failed(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *args: (_ for _ in ()).throw(RuntimeError("bad token")),
    )

    with pytest.raises(RuntimeError, match="bad token"):
        pr_orchestrator.push_and_create_pr(run_id)

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, push_error FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "push_failed"
    assert "bad token" in row[1]


def test_remote_mismatch_blocks_push(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name)
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "get_remote_url",
        lambda repo: "https://github.com/other/repo.git",
    )

    with pytest.raises(RuntimeError, match="origin remote does not match"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert not any(call[0] == "push" for call in calls)


def test_no_dangerous_git_or_github_scope():
    source = pr_orchestrator.__loader__.get_source(pr_orchestrator.__name__)

    assert "reset --hard" not in source
    assert "push --force" not in source
    assert "branch -d" not in source.lower()
    assert "delete_branch" not in source
