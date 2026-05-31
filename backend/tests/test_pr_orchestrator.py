"""
test_pr_orchestrator.py
Tests for Phase 2B-4D final push and PR creation.
No real push. No real GitHub calls.
"""

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
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


@pytest.fixture(autouse=True)
def github_encryption_key(monkeypatch):
    monkeypatch.setenv("PIPEWRIGHT_ENCRYPTION_KEY", Fernet.generate_key().decode())


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


def make_project(
    tmp_repo,
    monkeypatch=None,
    owner="acme",
    repo="demo",
    github_base_branch="pipewright-staging",
    pr_mode="manual_token",
):
    if monkeypatch is not None:
        monkeypatch.setenv("PIPEWRIGHT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return create_project(
        name=f"PR Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
        github_token="ghp_test",
        github_owner=owner,
        github_repo=repo,
        github_base_branch=github_base_branch,
        pr_mode=pr_mode,
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


def create_final_approved_run(
    tmp_repo,
    tracked_runs,
    status="final_approved",
    github_base_branch="pipewright-staging",
    pr_mode="manual_token",
):
    project = make_project(
        tmp_repo,
        github_base_branch=github_base_branch,
        pr_mode=pr_mode,
    )
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
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "commits_ahead",
        lambda base, branch, repo: calls.append(("ahead", base, branch, repo)) or 1,
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


def test_push_pr_rejects_branch_with_no_commits_ahead(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=False)
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "commits_ahead",
        lambda base, branch, repo: calls.append(("ahead", base, branch, repo)) or 0,
    )
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GitHub should not be called")
        ),
    )

    with pytest.raises(RuntimeError, match="Branch has no commits ahead of base"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert ("ahead", "pipewright-staging", branch_name, str(tmp_repo)) in calls
    assert not any(call[0] == "push" for call in calls)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, push_error
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    assert row[0] == "push_failed"
    assert "Branch has no commits ahead of base" in row[1]


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
        "base": "pipewright-staging",
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


def _read_run_row(run_id: str):
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT status, pr_url, pr_number, push_error
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()


def _forbid_push_and_github(monkeypatch):
    """Make any push or GitHub call fail the test loudly."""
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "push_branch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("push must not happen for a forbidden base branch")
        ),
    )
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GitHub must not be called for a forbidden base branch")
        ),
    )


@pytest.mark.parametrize("base", ["main", "master", "develop"])
def test_push_pr_rejects_forbidden_base_branch(monkeypatch, tmp_repo, tracked_runs, base):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, github_base_branch=base
    )
    _forbid_push_and_github(monkeypatch)

    with pytest.raises(RuntimeError, match="forbidden base branch"):
        pr_orchestrator.push_and_create_pr(run_id)

    row = _read_run_row(run_id)
    assert row[0] == "push_failed"
    assert row[1] is None  # no PR url
    assert base in row[3]


def test_push_pr_rejects_main_base_branch(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, github_base_branch="main"
    )
    _forbid_push_and_github(monkeypatch)

    with pytest.raises(RuntimeError, match="forbidden base branch: main"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert _read_run_row(run_id)[0] == "push_failed"


def test_push_pr_rejects_master_base_branch(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, github_base_branch="master"
    )
    _forbid_push_and_github(monkeypatch)

    with pytest.raises(RuntimeError, match="forbidden base branch: master"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert _read_run_row(run_id)[0] == "push_failed"


def test_push_pr_rejects_develop_base_branch(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, github_base_branch="develop"
    )
    _forbid_push_and_github(monkeypatch)

    with pytest.raises(RuntimeError, match="forbidden base branch: develop"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert _read_run_row(run_id)[0] == "push_failed"


def test_forbidden_base_branch_does_not_push_or_create_pr(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, github_base_branch="main"
    )
    calls = []
    branch_name = f"pipewright/{run_id[:8]}"
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=False)
    # Override push/github to detect any attempt; validation must run first.
    _forbid_push_and_github(monkeypatch)

    with pytest.raises(RuntimeError, match="forbidden base branch"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert not any(call[0] == "push" for call in calls)
    row = _read_run_row(run_id)
    assert row[1] is None  # no PR url
    assert row[2] is None  # no PR number


def test_push_pr_defaults_to_staging_branch(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = create_final_approved_run(tmp_repo, tracked_runs)
    # Force the stored base branch to NULL to exercise the orchestrator default.
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE projects SET github_base_branch = NULL WHERE id = :id
        """), {"id": project["id"]})
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=False)
    fake_repo = FakeRepo()
    patch_github(monkeypatch, fake_repo)

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert fake_repo.created[0]["base"] == "pipewright-staging"
    assert ("ahead", "pipewright-staging", branch_name, str(tmp_repo)) in calls


def test_push_error_is_sanitized(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    secret = "ghp_" + "A1b2C3d4e5" * 4  # token-like, > 32 chars
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"GitHub auth failed token={secret}")
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        pr_orchestrator.push_and_create_pr(run_id)

    # The re-raised HTTP-facing message must not leak the secret.
    assert secret not in str(exc_info.value)

    push_error = _read_run_row(run_id)[3]
    assert secret not in push_error
    assert "[REDACTED]" in push_error


def test_push_error_sanitizes_bare_tokenized_remote_url(
    monkeypatch, tmp_repo, tracked_runs
):
    # The real High-severity gap: a tokenized HTTPS remote in a push failure
    # has no "token="/"credential=" context word, so it relied on the new
    # GitHub-token / URL-credential redaction rather than _SECRET_CONTEXT_RE.
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    fake_token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    remote_url = f"https://{fake_token}@github.com/owner/repo.git"
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"fatal: unable to access {remote_url}")
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        pr_orchestrator.push_and_create_pr(run_id)

    assert fake_token not in str(exc_info.value)
    push_error = _read_run_row(run_id)[3]
    assert fake_token not in push_error
    assert "[REDACTED]" in push_error
    # Host/path stay useful for debugging.
    assert "github.com/owner/repo.git" in push_error


def test_run_payloads_never_expose_token(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(tmp_repo, tracked_runs)
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    secret = "ghp_" + "Z9y8X7w6v5" * 4
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"GitHub credential={secret} rejected")
        ),
    )
    with pytest.raises(RuntimeError):
        pr_orchestrator.push_and_create_pr(run_id)

    client = TestClient(app)

    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert "github_token" not in body
    assert secret not in detail.text
    assert "[REDACTED]" in body["push_error"]

    listing = client.get("/runs")
    assert listing.status_code == 200
    assert secret not in listing.text


def test_no_dangerous_git_or_github_scope():
    source = pr_orchestrator.__loader__.get_source(pr_orchestrator.__name__)

    assert "reset --hard" not in source
    assert "push --force" not in source
    assert "branch -d" not in source.lower()
    assert "delete_branch" not in source


# ---------------------------------------------------------------------------
# local_only mode
# ---------------------------------------------------------------------------

def _forbid_all_remote_action(monkeypatch):
    """Any push or GitHub/gh call must fail the test loudly."""
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "push_branch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("push must not happen in local_only mode")
        ),
    )
    monkeypatch.setattr(
        pr_orchestrator,
        "_get_github_repo",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("PyGithub must not be called in local_only mode")
        ),
    )
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "create_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("gh pr create must not be called in local_only mode")
        ),
    )
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "find_open_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("gh pr list must not be called in local_only mode")
        ),
    )


def test_local_only_does_not_push_or_create_pr(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="local_only"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    monkeypatch.setattr(pr_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        pr_orchestrator.local_git, "branch_exists", lambda branch, repo: True
    )
    _forbid_all_remote_action(monkeypatch)

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert result["pr_mode"] == "local_only"
    assert result["remote_action"] is False
    assert result["pr_url"] is None
    assert result["pr_number"] is None
    assert result["branch_name"] == branch_name

    row = _read_run_row(run_id)
    assert row[0] == "complete"
    assert row[1] is None  # no PR url
    assert row[2] is None  # no PR number
    assert row[3] is None  # no push_error: this is a safe outcome, not a failure


def test_local_only_returns_manual_pr_instructions(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="local_only"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    monkeypatch.setattr(pr_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        pr_orchestrator.local_git, "branch_exists", lambda branch, repo: True
    )
    _forbid_all_remote_action(monkeypatch)

    result = pr_orchestrator.push_and_create_pr(run_id)

    instructions = result["manual_instructions"]
    assert any("git checkout" in line for line in instructions)
    assert any(f"git push origin {branch_name}" in line for line in instructions)
    assert any("pull request" in line.lower() for line in instructions)
    assert "Local-only" in result["message"]


def test_local_only_does_not_require_github_token(monkeypatch, tmp_repo, tracked_runs):
    # A project with NO github_token / owner / repo still completes in local_only.
    project = create_project(
        name=f"Local Only {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
        pr_mode="local_only",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(run_id, project["id"], "Local only feature", make_triage(run_id, project["id"]))
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs SET status = 'final_approved' WHERE id = :run_id
        """), {"run_id": run_id})

    monkeypatch.setattr(pr_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        pr_orchestrator.local_git, "branch_exists", lambda branch, repo: True
    )
    _forbid_all_remote_action(monkeypatch)

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert result["pr_mode"] == "local_only"


# ---------------------------------------------------------------------------
# github_cli mode
# ---------------------------------------------------------------------------

def _patch_gh_ready(monkeypatch, installed=True, authenticated=True):
    monkeypatch.setattr(pr_orchestrator.gh_pr, "is_gh_installed", lambda: installed)
    monkeypatch.setattr(
        pr_orchestrator.gh_pr, "is_gh_authenticated", lambda: authenticated
    )


def test_github_cli_fails_safely_if_gh_missing(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="github_cli"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name)
    _patch_gh_ready(monkeypatch, installed=False, authenticated=False)
    # gh PR helpers must never be reached when gh is not ready.
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "create_pr",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("create_pr called")),
    )

    with pytest.raises(RuntimeError, match="gh is not installed or not authenticated"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert not any(call[0] == "push" for call in calls)
    row = _read_run_row(run_id)
    assert row[0] == "push_failed"
    assert "gh auth login" in row[3]


def test_github_cli_fails_safely_if_gh_unauthenticated(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="github_cli"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name)
    _patch_gh_ready(monkeypatch, installed=True, authenticated=False)

    with pytest.raises(RuntimeError, match="gh is not installed or not authenticated"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert not any(call[0] == "push" for call in calls)
    assert _read_run_row(run_id)[0] == "push_failed"


def test_github_cli_creates_pr_when_none_exists(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="github_cli"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=False)
    _patch_gh_ready(monkeypatch)
    monkeypatch.setattr(
        pr_orchestrator.gh_pr, "find_open_pr", lambda repo, branch, base: None
    )
    created = {}

    def fake_create(repo, branch, base, title, body):
        created.update({"repo": repo, "branch": branch, "base": base, "title": title})
        return {"url": "https://github.com/acme/demo/pull/321", "number": 321, "title": title}

    monkeypatch.setattr(pr_orchestrator.gh_pr, "create_pr", fake_create)

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == "complete"
    assert result["pr_number"] == 321
    assert created["branch"] == branch_name
    assert created["base"] == "pipewright-staging"
    assert ("push", branch_name, str(tmp_repo)) in calls
    row = _read_run_row(run_id)
    assert row[0] == "complete"
    assert row[2] == 321


def test_github_cli_reuses_existing_pr(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="github_cli"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    _patch_gh_ready(monkeypatch)
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "find_open_pr",
        lambda repo, branch, base: {
            "url": "https://github.com/acme/demo/pull/55",
            "number": 55,
            "title": "Existing",
        },
    )
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "create_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_pr must not be called when a PR exists")
        ),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["pr_number"] == 55
    row = _read_run_row(run_id)
    assert row[0] == "complete"
    assert row[1].endswith("/55")


def test_github_cli_errors_are_sanitized(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="github_cli"
    )
    branch_name = f"pipewright/{run_id[:8]}"
    calls = []
    patch_git_for_branch(monkeypatch, calls, branch_name, remote_exists=True)
    _patch_gh_ready(monkeypatch)
    secret = "ghp_" + "A1b2C3d4e5" * 4  # token-like, > 32 chars
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "find_open_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            pr_orchestrator.gh_pr.GhCliError(f"gh failed token={secret}")
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        pr_orchestrator.push_and_create_pr(run_id)

    assert secret not in str(exc_info.value)
    push_error = _read_run_row(run_id)[3]
    assert secret not in push_error
    assert "[REDACTED]" in push_error


def test_github_cli_rejects_forbidden_base_branch(monkeypatch, tmp_repo, tracked_runs):
    run_id, _project = create_final_approved_run(
        tmp_repo, tracked_runs, pr_mode="github_cli", github_base_branch="main"
    )
    # gh and push must never be reached for a forbidden base branch.
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "ensure_gh_ready",
        lambda: (_ for _ in ()).throw(AssertionError("gh must not be probed")),
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "push_branch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("push must not happen")),
    )

    with pytest.raises(RuntimeError, match="forbidden base branch"):
        pr_orchestrator.push_and_create_pr(run_id)

    row = _read_run_row(run_id)
    assert row[0] == "push_failed"
    assert row[1] is None
