"""
test_pr_checks.py
Tests for the read-only, display-only PR checks foundation (#31D).

No real GitHub. The gh layer is exercised with mocked subprocess output; the
aggregation and read-model surfacing are pure. These assert the safety-critical
honesty rule: a gh/network failure reads as ``unavailable``, never ``failed``,
and checks are surfaced only when a PR actually exists.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.git import gh_pr
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import pr_checks
from backend.pipeline.chunk_store import create_chunked_run
from backend.pipeline.pr_checks import (
    ChecksState,
    fetch_checks_summary,
    summarize_checks,
)
from backend.projects.project_store import create_project
from backend.routes import chunks as chunks_routes

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# summarize_checks (pure)                                                      #
# --------------------------------------------------------------------------- #


def _checks(*buckets):
    return [{"name": f"c{i}", "bucket": b} for i, b in enumerate(buckets)]


def test_summarize_empty_is_no_checks():
    summary = summarize_checks([])
    assert summary.state == ChecksState.NO_CHECKS
    assert summary.total == 0


def test_summarize_none_is_no_checks():
    summary = summarize_checks(None)
    assert summary.state == ChecksState.NO_CHECKS


def test_summarize_all_pass_is_passed():
    summary = summarize_checks(_checks("pass", "pass"))
    assert summary.state == ChecksState.PASSED
    assert summary.total == 2
    assert summary.passed == 2
    assert summary.failed == 0


def test_summarize_any_fail_is_failed():
    summary = summarize_checks(_checks("pass", "fail", "pending"))
    # Failure wins over pending and pass.
    assert summary.state == ChecksState.FAILED
    assert summary.failed == 1
    assert summary.pending == 1
    assert summary.passed == 1


def test_summarize_pending_without_fail_is_pending():
    summary = summarize_checks(_checks("pass", "pending"))
    assert summary.state == ChecksState.PENDING
    assert summary.pending == 1


def test_summarize_cancel_counts_as_failed():
    summary = summarize_checks(_checks("cancel"))
    assert summary.state == ChecksState.FAILED
    assert summary.failed == 1


def test_summarize_skipping_is_skipped_and_passes_when_only_skips():
    summary = summarize_checks(_checks("skipping", "skipping"))
    assert summary.skipped == 2
    # No failures, nothing pending -> not red, not pending.
    assert summary.state == ChecksState.PASSED


def test_summarize_unknown_bucket_counts_as_skipped():
    summary = summarize_checks(_checks("weird_new_bucket"))
    assert summary.skipped == 1
    assert summary.failed == 0
    assert summary.pending == 0


def test_summarize_stamps_checked_at():
    summary = summarize_checks(_checks("pass"), checked_at="2026-06-04T00:00:00+00:00")
    assert summary.checked_at == "2026-06-04T00:00:00+00:00"
    dumped = summary.model_dump()
    assert dumped["state"] == ChecksState.PASSED
    assert dumped["checked_at"] == "2026-06-04T00:00:00+00:00"
    assert dumped["schema_version"] == pr_checks.SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# fetch_checks_summary (failure => unavailable, never failed)                 #
# --------------------------------------------------------------------------- #


def test_fetch_uses_injected_fetcher():
    summary = fetch_checks_summary(
        "/repo",
        42,
        fetcher=lambda repo, ident: _checks("pass", "fail"),
    )
    assert summary.state == ChecksState.FAILED


def test_fetch_failure_is_unavailable_not_failed():
    def boom(repo, ident):
        raise RuntimeError("gh exploded")

    summary = fetch_checks_summary("/repo", 42, fetcher=boom)
    assert summary.state == ChecksState.UNAVAILABLE
    # An unavailable summary must never be mistaken for a failing build.
    assert summary.failed == 0
    assert summary.total == 0


def test_fetch_none_result_is_unavailable():
    summary = fetch_checks_summary("/repo", 42, fetcher=lambda repo, ident: None)
    assert summary.state == ChecksState.UNAVAILABLE


def test_fetch_passes_identifier_as_string():
    seen = {}

    def fetcher(repo, ident):
        seen["repo"] = repo
        seen["ident"] = ident
        return _checks("pass")

    fetch_checks_summary("/repo", 99, fetcher=fetcher)
    assert seen["repo"] == "/repo"
    assert seen["ident"] == "99"


# --------------------------------------------------------------------------- #
# gh_pr.get_pr_checks (mocked subprocess)                                      #
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_get_pr_checks_parses_json_even_on_nonzero_exit(monkeypatch):
    # gh exits non-zero when checks are pending/failing; the JSON is still
    # authoritative and must be parsed.
    payload = '[{"name":"build","bucket":"fail"}]'
    monkeypatch.setattr(
        gh_pr, "_run_gh", lambda args, repo, timeout=30: _FakeProc(stdout=payload, returncode=1)
    )
    rows = gh_pr.get_pr_checks("/repo", 7)
    assert rows == [{"name": "build", "bucket": "fail"}]


def test_get_pr_checks_no_checks_marker_returns_empty(monkeypatch):
    monkeypatch.setattr(
        gh_pr,
        "_run_gh",
        lambda args, repo, timeout=30: _FakeProc(
            stdout="", stderr="no checks reported on the 'x' branch", returncode=1
        ),
    )
    assert gh_pr.get_pr_checks("/repo", 7) == []


def test_get_pr_checks_empty_without_marker_raises(monkeypatch):
    monkeypatch.setattr(
        gh_pr,
        "_run_gh",
        lambda args, repo, timeout=30: _FakeProc(stdout="", stderr="boom", returncode=1),
    )
    with pytest.raises(gh_pr.GhCliError):
        gh_pr.get_pr_checks("/repo", 7)


def test_get_pr_checks_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(
        gh_pr,
        "_run_gh",
        lambda args, repo, timeout=30: _FakeProc(stdout="not json", returncode=0),
    )
    with pytest.raises(gh_pr.GhCliError):
        gh_pr.get_pr_checks("/repo", 7)


def test_get_pr_checks_via_default_fetcher(monkeypatch):
    # fetch_checks_summary -> _default_fetcher -> gh_pr.get_pr_checks
    monkeypatch.setattr(
        gh_pr,
        "_run_gh",
        lambda args, repo, timeout=30: _FakeProc(
            stdout='[{"name":"t","bucket":"pass"}]', returncode=0
        ),
    )
    summary = fetch_checks_summary("/repo", 5)
    assert summary.state == ChecksState.PASSED
    assert summary.passed == 1


# --------------------------------------------------------------------------- #
# read-model wiring: checks surface only with an explicit fetcher AND a PR     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def tracked_runs():
    run_ids = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM chunks WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})


def _make_triage(run_id, project_id):
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Checks foundation",
        complexity="easy",
        total_chunks=1,
        reasoning="one chunk",
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="Only chunk",
                description="do it",
                files_expected=["a.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=10,
                requires_human_review=False,
                rationale="x",
            )
        ],
    )


def _make_run(tmp_repo, tracked_runs, status, *, pr_url=None, pr_number=None):
    project = create_project(
        name=f"Checks {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
        pr_mode="github_cli",
        github_owner="acme",
        github_repo="demo",
        github_base_branch="pipewright-staging",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(run_id, project["id"], "Checks foundation", _make_triage(run_id, project["id"]))
    with engine.begin() as conn:
        conn.execute(text("UPDATE chunks SET status='completed' WHERE run_id=:r"), {"r": run_id})
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                chunk_plan_status = 'approved',
                total_chunks = 1,
                branch_name = :branch,
                pr_url = :pr_url,
                pr_number = :pr_number
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "status": status,
            "branch": f"pipewright/{run_id[:8]}",
            "pr_url": pr_url,
            "pr_number": pr_number,
        })
    return run_id


def test_read_model_surfaces_checks_when_pr_exists(tmp_repo, tracked_runs):
    run_id = _make_run(
        tmp_repo,
        tracked_runs,
        status="complete",
        pr_url="https://github.com/acme/demo/pull/12",
        pr_number=12,
    )
    plan = chunks_routes.get_chunk_plan_status(run_id)

    def fetcher(repo, ident):
        return _checks("pass", "pass")

    result = chunks_routes._augment_plan_with_pr_status(plan, checks_fetcher=fetcher)

    checks = result.pr_status["checks"]
    assert checks is not None
    assert checks["state"] == ChecksState.PASSED
    assert checks["total"] == 2


def test_read_model_default_load_does_not_fetch_checks(tmp_repo, tracked_runs):
    run_id = _make_run(
        tmp_repo,
        tracked_runs,
        status="complete",
        pr_url="https://github.com/acme/demo/pull/12",
        pr_number=12,
    )
    plan = chunks_routes.get_chunk_plan_status(run_id)

    # No fetcher supplied (the default route behavior): never call GitHub.
    result = chunks_routes._augment_plan_with_pr_status(plan)

    assert result.pr_status["pr_state"] == "pr_open"
    assert result.pr_status["checks"] is None


def test_read_model_no_pr_means_no_checks_even_with_fetcher(tmp_repo, tracked_runs):
    run_id = _make_run(tmp_repo, tracked_runs, status="final_approved")
    plan = chunks_routes.get_chunk_plan_status(run_id)

    called = {"n": 0}

    def fetcher(repo, ident):
        called["n"] += 1
        return _checks("pass")

    result = chunks_routes._augment_plan_with_pr_status(plan, checks_fetcher=fetcher)

    # No PR -> no fetch attempted, no checks surfaced.
    assert called["n"] == 0
    assert result.pr_status["checks"] is None


def test_read_model_checks_unavailable_when_fetch_fails(tmp_repo, tracked_runs):
    run_id = _make_run(
        tmp_repo,
        tracked_runs,
        status="complete",
        pr_url="https://github.com/acme/demo/pull/12",
        pr_number=12,
    )
    plan = chunks_routes.get_chunk_plan_status(run_id)

    def boom(repo, ident):
        raise RuntimeError("gh failed")

    result = chunks_routes._augment_plan_with_pr_status(plan, checks_fetcher=boom)

    assert result.pr_status["checks"]["state"] == ChecksState.UNAVAILABLE
