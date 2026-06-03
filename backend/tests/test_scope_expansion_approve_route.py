"""
test_scope_expansion_approve_route.py
Tests for the scope expansion approve-and-retry route + locked orchestrator
(#27E).

Approve-and-retry is the most sensitive #27 slice. Scope approval is NOT code
approval: it only authorizes retrying under a wider allowlist, and a successful
expanded retry still pauses at awaiting_chunk_approval (committed later by the
unchanged approval path). These tests prove:

  - a pending request can be approved and the retry is invoked under the amended
    effective scope (original ∪ approved extras);
  - approved_files are validated (subset / non-empty / non-forbidden) and
    persisted, and pending -> approved happens only after the branch precheck;
  - a wrong branch returns 409 and leaves the request pending (no retry);
  - an approved-but-not-applied request re-drives the retry (crash window);
  - applied / rejected / superseded requests cannot be approved again;
  - wrong run/chunk/request and stale failure_report_id mutate nothing;
  - chunks.files_expected stays immutable; the raw column never changes;
  - a successful retry pauses at awaiting_chunk_approval and never commits;
  - a retry failure persists normally; a re-failed SCOPE_VIOLATION respects the
    MAX_SCOPE_AMENDMENTS cap (no second pending request);
  - _execute_retry_attempt is never called when a precheck fails;
  - #26's public retry eligibility still rejects SCOPE_VIOLATION.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import chunked_orchestrator
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
    save_chunk_completion_summary,
    update_chunk_status,
)
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
    evaluate_patch_retry_eligibility,
    patch_failure_report_to_completion_summary,
)
from backend.pipeline.scope_expansion import ScopeExpansionStatus, is_in_force
from backend.pipeline.scope_expansion_store import (
    create_scope_expansion_request,
    get_scope_expansion_request,
    list_in_force_scope_expansion_files,
    list_scope_expansion_requests_for_chunk,
    update_scope_expansion_request_status,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM scope_expansion_requests WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM chunks WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})


def _client() -> TestClient:
    return TestClient(app)


def _set_run_status(run_id: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET status = :s WHERE id = :r"),
            {"s": status, "r": run_id},
        )


def _run_status(run_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :r"), {"r": run_id}
        ).fetchone()[0]


def _chunk_status(run_id: str, chunk_number: int) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM chunks WHERE run_id = :r AND chunk_number = :c"),
            {"r": run_id, "c": chunk_number},
        ).fetchone()[0]


def _raw_files_expected(run_id: str, chunk_number: int = 1) -> list[str]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT files_expected FROM chunks WHERE run_id = :r AND chunk_number = :c"),
            {"r": run_id, "c": chunk_number},
        ).fetchone()
    return json.loads(row[0])


def _seed(tmp_repo, tracked_runs, *, chunks=1):
    project = create_project(
        name=f"Approve {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    definitions = []
    for number in range(1, chunks + 1):
        definitions.append(ChunkDefinition(
            chunk_number=number,
            title=f"Chunk {number}",
            description=f"Do {number}",
            files_expected=["a.py"] if number == 1 else [f"file_{number}.py"],
            depends_on=[] if number == 1 else [number - 1],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="r",
        ))
    triage = TriageResult(
        run_id=run_id,
        project_id=project["id"],
        feature_description="Feature",
        complexity="easy" if chunks == 1 else "medium",
        total_chunks=chunks,
        chunks=definitions,
        reasoning="split",
    )
    create_chunked_run(run_id, project["id"], "Feature", triage)
    approve_chunk_plan(run_id)
    return run_id, project["id"]


def _store_failure_report(run_id, chunk_number, *, frid, attempted, allowed):
    """Persist a clean SCOPE_VIOLATION patch-failure report on a chunk."""
    report = build_patch_failure_report(
        PatchFailureType.SCOPE_VIOLATION,
        changed_files_attempted=list(attempted),
        allowed_files=list(allowed),
        working_tree_clean=True,
        chunk_number=chunk_number,
    ).model_copy(update={"failure_report_id": frid})
    update_chunk_status(run_id, chunk_number, "failed", "scope violation")
    save_chunk_completion_summary(
        run_id, chunk_number, patch_failure_report_to_completion_summary(report)
    )


def _seed_pending(
    tmp_repo,
    tracked_runs,
    *,
    chunks=1,
    frid="frid-1",
    requested=("src/extra.py",),
    report_frid=None,
):
    """
    Seed an approved plan with chunk 1 failed on a clean SCOPE_VIOLATION, the run
    surfaced as awaiting_scope_approval, and a single pending scope expansion
    request tied to ``frid``. ``report_frid`` overrides the failure_report_id
    stored on the chunk (to simulate a stale request).
    """
    run_id, project_id = _seed(tmp_repo, tracked_runs, chunks=chunks)
    _store_failure_report(
        run_id, 1, frid=report_frid or frid, attempted=requested, allowed=["a.py"]
    )
    _set_run_status(run_id, "awaiting_scope_approval")
    request = create_scope_expansion_request(
        run_id, project_id, 1, frid, requested_files=list(requested),
    )
    return run_id, project_id, request.id


def _patch_git(monkeypatch, run_id, *, current_branch=None, clean=True):
    """Make the orchestrator's git reads deterministic without a real repo."""
    current = (
        current_branch if current_branch is not None
        else f"pipewright/{run_id[:8]}"
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "get_current_branch", lambda repo: current
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: clean
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "ensure_git_repo", lambda repo: None
    )

    def _boom_commit(*args, **kwargs):
        raise AssertionError("commit must not be called during scope retry")

    monkeypatch.setattr(chunked_orchestrator.local_git, "commit_files", _boom_commit)


def _patch_retry_success(monkeypatch, captured):
    async def fake(run_id, chunk_number, chunk, plan_status, project_runtime,
                   target_repo_path, prior_attempts, branch_name, report):
        captured["called"] = True
        captured["files_expected"] = list(chunk.files_expected)
        # Mirror the real success path's observable effects: pause at the chunk
        # approval gate WITHOUT committing.
        chunked_orchestrator.update_chunk_status(
            run_id, chunk_number, "awaiting_chunk_approval"
        )
        chunked_orchestrator._update_run_status(
            run_id, "awaiting_chunk_approval",
            f"chunk_{chunk_number}_recovered", chunk_number,
        )
        return {
            "status": "awaiting_chunk_approval",
            "run_id": run_id,
            "chunk_number": chunk_number,
            "failure_report_id": "recovered-frid",
        }

    monkeypatch.setattr(chunked_orchestrator, "_execute_retry_attempt", fake)


def _patch_retry_failure(monkeypatch, captured, *, failure_type):
    async def fake(run_id, chunk_number, chunk, plan_status, project_runtime,
                   target_repo_path, prior_attempts, branch_name, report):
        captured["called"] = True
        captured["files_expected"] = list(chunk.files_expected)
        fail_report = build_patch_failure_report(
            failure_type,
            changed_files_attempted=["a.py"],
            allowed_files=list(chunk.files_expected),
            working_tree_clean=True,
            chunk_number=chunk_number,
        )
        # Genuinely persist the failure via the real #26 retry-failure seam.
        return chunked_orchestrator._persist_retry_patch_failure(
            run_id, chunk_number, fail_report, prior_attempts
        )

    monkeypatch.setattr(chunked_orchestrator, "_execute_retry_attempt", fake)


def _patch_retry_boom(monkeypatch):
    async def fake(*args, **kwargs):
        raise AssertionError("_execute_retry_attempt must not be called")

    monkeypatch.setattr(chunked_orchestrator, "_execute_retry_attempt", fake)


def _approve(run_id, request_id, approved_files, *, chunk=1, reason="needed"):
    return _client().post(
        f"/runs/{run_id}/chunks/{chunk}/scope-expansion/{request_id}/approve",
        json={"approved_files": approved_files, "reason": reason},
    )


# 1 / 2 / 3: pending approved -> retry invoked, approved_files persisted, flip
def test_pending_request_approved_and_retry_invoked(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    captured: dict = {}
    _patch_retry_success(monkeypatch, captured)

    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_chunk_approval"
    assert captured["called"] is True
    request = get_scope_expansion_request(request_id)
    # pending -> approved -> applied; the approved allowlist is persisted.
    assert request.status == ScopeExpansionStatus.APPLIED.value
    assert request.approved_files == ["src/extra.py"]
    assert request.decision_reason == "needed"


# 15: effective scope (original ∪ approved) reaches the retry
def test_effective_scope_includes_approved_files(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    captured: dict = {}
    _patch_retry_success(monkeypatch, captured)

    _approve(run_id, request_id, ["src/extra.py"])

    assert captured["files_expected"] == ["a.py", "src/extra.py"]


# 16 / 17: successful retry pauses at awaiting_chunk_approval and never commits
def test_successful_retry_pauses_without_commit(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)  # commit_files is patched to raise
    captured: dict = {}
    _patch_retry_success(monkeypatch, captured)

    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 200
    assert _chunk_status(run_id, 1) == "awaiting_chunk_approval"


# 14: chunks.files_expected raw column stays immutable
def test_raw_files_expected_unchanged_after_approve(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_success(monkeypatch, {})

    _approve(run_id, request_id, ["src/extra.py"])

    assert _raw_files_expected(run_id, 1) == ["a.py"]
    # Effective scope is overlaid, not persisted onto the chunk row.
    assert get_chunk_plan_status(run_id).chunks[0].files_expected == [
        "a.py", "src/extra.py",
    ]


# 4: wrong branch -> 409, request stays pending, retry not called
def test_wrong_branch_returns_409_and_keeps_pending(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id, current_branch="main")
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 409
    assert response.json()["status"] == "retry_ineligible"
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value
    assert get_scope_expansion_request(request_id).approved_files == []


# 5: approved-but-not-applied request re-drives the retry (crash window)
def test_approved_not_applied_redrives_retry(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    # Simulate a crash after pending -> approved but before the retry wrote a
    # result: the request is approved (in force) and the chunk is still failed.
    update_scope_expansion_request_status(
        request_id, ScopeExpansionStatus.APPROVED, approved_files=["src/extra.py"]
    )
    _patch_git(monkeypatch, run_id)
    captured: dict = {}
    _patch_retry_success(monkeypatch, captured)

    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 200
    assert captured["called"] is True
    # Re-drive uses the already-approved scope and ends applied.
    assert captured["files_expected"] == ["a.py", "src/extra.py"]
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.APPLIED.value


# 6 / 7 / 8: applied / rejected / superseded cannot be approved again
@pytest.mark.parametrize("terminal", ["applied", "rejected", "superseded"])
def test_non_redrivable_status_returns_409(tmp_repo, tracked_runs, monkeypatch, terminal):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    if terminal == "applied":
        update_scope_expansion_request_status(
            request_id, ScopeExpansionStatus.APPROVED, approved_files=["src/extra.py"]
        )
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.APPLIED)
    elif terminal == "rejected":
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.REJECTED)
    else:
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.SUPERSEDED)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    before = get_scope_expansion_request(request_id)
    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 409
    after = get_scope_expansion_request(request_id)
    assert after.status == before.status


# 9: wrong request/run/chunk mutate nothing
def test_wrong_request_id_returns_404(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, str(uuid.uuid4()), ["src/extra.py"])

    assert response.status_code == 404
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


def test_wrong_run_id_returns_404(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _client().post(
        f"/runs/{uuid.uuid4()}/chunks/1/scope-expansion/{request_id}/approve",
        json={"approved_files": ["src/extra.py"]},
    )

    assert response.status_code == 404
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


def test_wrong_chunk_number_returns_404(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, ["src/extra.py"], chunk=99)

    assert response.status_code == 404
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 10: stale failure_report_id -> 409, mutate nothing
def test_stale_failure_report_id_returns_409(tmp_repo, tracked_runs, monkeypatch):
    # The chunk's stored report carries a NEWER failure_report_id than the request.
    run_id, _project_id, request_id = _seed_pending(
        tmp_repo, tracked_runs, frid="frid-old", report_frid="frid-new",
    )
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 409
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 11: approved_files outside requested_files -> 422, pending
def test_approved_files_outside_requested_returns_422(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, ["src/not_requested.py"])

    assert response.status_code == 422
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 12: empty approved_files -> 422, pending
def test_empty_approved_files_returns_422(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, [])

    assert response.status_code == 422
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 13: forbidden/high-risk approved file -> 422, pending
def test_forbidden_approved_file_returns_422(tmp_repo, tracked_runs, monkeypatch):
    # requested includes a requestable file (so eligibility passes) plus a
    # forbidden one; approving the forbidden one must be rejected.
    run_id, _project_id, request_id = _seed_pending(
        tmp_repo, tracked_runs, requested=("src/extra.py", ".env"),
    )
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, [".env"])

    assert response.status_code == 422
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 20: a precheck failure never reaches retry execution (covered above via
# _patch_retry_boom); this pins the side-effect-free contract explicitly.
def test_validation_failure_does_not_run_retry(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_boom(monkeypatch)

    response = _approve(run_id, request_id, [])

    assert response.status_code == 422
    # Chunk untouched: still failed, never marked running/awaiting.
    assert _chunk_status(run_id, 1) == "failed"


# 18: a retry failure persists normally (new failure report), request applied
def test_retry_failure_persists_normally(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    captured: dict = {}
    _patch_retry_failure(
        monkeypatch, captured, failure_type=PatchFailureType.TEST_FAILURE_AFTER_APPLY
    )

    response = _approve(run_id, request_id, ["src/extra.py"])

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failure_report_id"]  # a fresh id was minted
    assert _chunk_status(run_id, 1) == "failed"
    # The amendment was consumed -> applied.
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.APPLIED.value


# 19: a re-failed SCOPE_VIOLATION respects MAX_SCOPE_AMENDMENTS (no 2nd pending)
def test_refailed_scope_violation_respects_cap(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    captured: dict = {}
    _patch_retry_failure(
        monkeypatch, captured, failure_type=PatchFailureType.SCOPE_VIOLATION
    )

    _approve(run_id, request_id, ["src/extra.py"])

    requests = list_scope_expansion_requests_for_chunk(run_id, 1)
    pending = [r for r in requests if r.status == ScopeExpansionStatus.PENDING.value]
    # The one amendment is spent (applied/in force) so no new pending request is
    # created — the chunk routes to manual intervention instead.
    assert pending == []
    assert any(r.status == ScopeExpansionStatus.APPLIED.value for r in requests)


# safety: an approved request is in force; its approved_files feed effective scope
def test_approved_request_is_in_force(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    _patch_git(monkeypatch, run_id)
    _patch_retry_success(monkeypatch, {})

    _approve(run_id, request_id, ["src/extra.py"])

    request = get_scope_expansion_request(request_id)
    assert is_in_force(request.status) is True
    assert list_in_force_scope_expansion_files(run_id, 1) == ["src/extra.py"]


# 21: #26 public retry eligibility still rejects SCOPE_VIOLATION
def test_hash26_retry_still_rejects_scope_violation():
    report = build_patch_failure_report(
        PatchFailureType.SCOPE_VIOLATION,
        changed_files_attempted=["src/extra.py"],
        allowed_files=["a.py"],
        working_tree_clean=True,
        chunk_number=1,
    ).model_copy(update={"failure_report_id": "frid-1"})

    decision = evaluate_patch_retry_eligibility(
        report,
        requested_failure_report_id="frid-1",
        dependencies_met=True,
        working_tree_clean=True,
        chunk_status="failed",
    )

    assert decision.eligible is False
    assert decision.reason == "disallowed_failure_type"
