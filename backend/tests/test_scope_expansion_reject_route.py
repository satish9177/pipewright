"""
test_scope_expansion_reject_route.py
Tests for the scope expansion reject route + store lifecycle (#27 reject slice).

Reject is NOT retry and NOT code approval. These tests prove:
  - a pending request can be rejected (pending -> rejected) with reason/decided_at;
  - a rejected request is not in force and never affects effective files_expected;
  - chunks.files_expected stays immutable and the chunk stays failed;
  - dependents stay blocked;
  - the run is settled off awaiting_scope_approval back to failed;
  - wrong run/chunk/request and non-pending statuses are rejected with clear
    404/409 and mutate nothing;
  - no retry execution and no commit happen;
  - #26 retry eligibility still rejects SCOPE_VIOLATION.
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
from backend.pipeline.chunk_store import create_chunked_run, get_chunk_plan_status, update_chunk_status
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
    evaluate_patch_retry_eligibility,
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
            conn.execute(text("DELETE FROM chunk_attempts WHERE run_id = :r"), {"r": run_id})
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
        name=f"Reject {uuid.uuid4()}",
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
            files_expected=[f"file_{number}.py"] if number != 1 else ["a.py"],
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
    return run_id, project["id"]


def _seed_pending(tmp_repo, tracked_runs, *, chunks=1, frid="frid-1"):
    run_id, project_id = _seed(tmp_repo, tracked_runs, chunks=chunks)
    update_chunk_status(run_id, 1, "failed", "scope violation")
    _set_run_status(run_id, "awaiting_scope_approval")
    request = create_scope_expansion_request(
        run_id, project_id, 1, frid, requested_files=["src/extra.py"],
    )
    return run_id, project_id, request.id


# 1 / 2
def test_pending_request_can_be_rejected(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    response = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject",
        json={"reason": "Keep it inside the original scope."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "scope_expansion_rejected"
    assert body["request"]["status"] == ScopeExpansionStatus.REJECTED.value
    request = get_scope_expansion_request(request_id)
    assert request.status == ScopeExpansionStatus.REJECTED.value
    assert request.decision_reason == "Keep it inside the original scope."
    assert request.decided_at is not None


# 3 / safety: approved_files stays empty, request not in force
def test_rejected_request_is_not_in_force(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    _client().post(f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={})

    request = get_scope_expansion_request(request_id)
    assert is_in_force(request.status) is False
    assert request.approved_files == []
    assert list_in_force_scope_expansion_files(run_id, 1) == []


# 4 / 5 / 6
def test_reject_does_not_change_scope_or_chunk(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    _client().post(f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={})

    assert get_chunk_plan_status(run_id).chunks[0].files_expected == ["a.py"]
    assert _raw_files_expected(run_id, 1) == ["a.py"]
    assert _chunk_status(run_id, 1) == "failed"


# 7
def test_dependent_chunk_stays_blocked(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs, chunks=2)

    _client().post(f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={})

    # Chunk 1 stays failed -> chunk 2 (depends on 1) never runs.
    assert _chunk_status(run_id, 1) == "failed"
    assert _chunk_status(run_id, 2) == "pending"


# 8
def test_run_settled_off_awaiting_scope_approval(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    assert _run_status(run_id) == "awaiting_scope_approval"

    _client().post(f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={})

    assert _run_status(run_id) == "failed"


# 9
def test_wrong_request_id_returns_404_and_mutates_nothing(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    response = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{uuid.uuid4()}/reject", json={},
    )

    assert response.status_code == 404
    # The real request is untouched.
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value
    assert _run_status(run_id) == "awaiting_scope_approval"


# 10
def test_wrong_run_id_returns_404_and_mutates_nothing(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    response = _client().post(
        f"/runs/{uuid.uuid4()}/chunks/1/scope-expansion/{request_id}/reject", json={},
    )

    assert response.status_code == 404
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 11
def test_wrong_chunk_number_returns_404_and_mutates_nothing(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    response = _client().post(
        f"/runs/{run_id}/chunks/99/scope-expansion/{request_id}/reject", json={},
    )

    assert response.status_code == 404
    assert get_scope_expansion_request(request_id).status == ScopeExpansionStatus.PENDING.value


# 12
@pytest.mark.parametrize("seed_status", ["approved", "applied", "rejected", "superseded"])
def test_rejecting_non_pending_returns_409_and_mutates_nothing(tmp_repo, tracked_runs, seed_status):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)
    # Drive the request into a non-pending status via legal transitions.
    if seed_status == "approved":
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.APPROVED, approved_files=["src/extra.py"])
    elif seed_status == "applied":
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.APPROVED, approved_files=["src/extra.py"])
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.APPLIED)
    elif seed_status == "rejected":
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.REJECTED)
    elif seed_status == "superseded":
        update_scope_expansion_request_status(request_id, ScopeExpansionStatus.SUPERSEDED)

    before = get_scope_expansion_request(request_id)
    response = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={"reason": "x"},
    )

    assert response.status_code == 409
    after = get_scope_expansion_request(request_id)
    assert after.status == before.status
    assert after.decision_reason == before.decision_reason


# 13
def test_double_reject_is_409(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    first = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={"reason": "first"},
    )
    second = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={"reason": "second"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    # The original rejection reason is preserved; the second call changed nothing.
    assert get_scope_expansion_request(request_id).decision_reason == "first"


# 14
def test_missing_or_empty_reason_stores_default(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    # Empty body (no reason) is accepted, matching the other reject endpoints.
    response = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={},
    )

    assert response.status_code == 200
    assert get_scope_expansion_request(request_id).decision_reason == "Scope expansion rejected."


def test_blank_reason_stores_default(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    response = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={"reason": "   "},
    )

    assert response.status_code == 200
    assert get_scope_expansion_request(request_id).decision_reason == "Scope expansion rejected."


# 15 / 16: no retry execution, no commit
def test_reject_does_not_retry_or_commit(tmp_repo, tracked_runs, monkeypatch):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    async def _boom_retry(*args, **kwargs):
        raise AssertionError("human_retry driver must not be called on reject")

    def _boom_commit(*args, **kwargs):
        raise AssertionError("commit must not be called on reject")

    monkeypatch.setattr(chunked_orchestrator.chunk_driver, "drive_chunk", _boom_retry)
    monkeypatch.setattr(chunked_orchestrator.local_git, "commit_files", _boom_commit)

    response = _client().post(
        f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={"reason": "no"},
    )

    assert response.status_code == 200


# safety: no approved/applied rows created by reject
def test_reject_creates_no_approved_or_applied_rows(tmp_repo, tracked_runs):
    run_id, _project_id, request_id = _seed_pending(tmp_repo, tracked_runs)

    _client().post(f"/runs/{run_id}/chunks/1/scope-expansion/{request_id}/reject", json={})

    statuses = [r.status for r in list_scope_expansion_requests_for_chunk(run_id, 1)]
    assert ScopeExpansionStatus.APPROVED.value not in statuses
    assert ScopeExpansionStatus.APPLIED.value not in statuses


# 17: #26 retry eligibility still rejects SCOPE_VIOLATION
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
