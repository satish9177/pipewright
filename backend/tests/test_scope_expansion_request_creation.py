"""
test_scope_expansion_request_creation.py
Tests for creating a pending scope_expansion_request on an eligible clean
SCOPE_VIOLATION (#27 — request creation slice).

These tests prove ONLY request creation/surfacing:
  - an eligible clean SCOPE_VIOLATION creates exactly one pending request;
  - ineligible failures (dirty tree, manual intervention, non-scope, all-forbidden
    extras, cap-exhausted) create nothing;
  - the pending request never affects effective scope and never mutates
    chunks.files_expected;
  - duplicate persistence is idempotent and a newer failure supersedes an older
    pending request;
  - the run is surfaced as awaiting_scope_approval (chunk stays failed);
  - #26's public retry eligibility still rejects SCOPE_VIOLATION.

No approval, no retry execution, no commit, no routes, no frontend are exercised.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import chunked_orchestrator
from backend.pipeline.chunk_store import create_chunked_run, get_chunk_plan_status
from backend.pipeline.patch_failures import (
    PatchFailureReport,
    PatchFailureType,
    build_patch_failure_report,
    evaluate_patch_retry_eligibility,
)
from backend.pipeline.scope_expansion import ScopeExpansionStatus
from backend.pipeline.scope_expansion_store import (
    count_in_force_scope_amendments,
    list_scope_expansion_requests_for_chunk,
    maybe_create_scope_expansion_request_for_failure,
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
            conn.execute(
                text("DELETE FROM scope_expansion_requests WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {"run_id": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {"run_id": run_id})


def _seed_run(tmp_repo, tracked_runs, files=("a.py",)):
    project = create_project(
        name=f"Scope Create {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = TriageResult(
        run_id=run_id,
        project_id=project["id"],
        feature_description="Feature",
        complexity="easy",
        total_chunks=1,
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Chunk 1",
            description="Do chunk 1",
            files_expected=list(files),
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="r",
        )],
        reasoning="split",
    )
    create_chunked_run(run_id, project["id"], "Feature", triage)
    return run_id, project["id"]


def _report(
    *,
    failure_type=PatchFailureType.SCOPE_VIOLATION,
    attempted=("src/extra.py",),
    actual=(),
    allowed=("a.py",),
    clean=True,
    manual=False,
    failure_report_id="frid-1",
) -> PatchFailureReport:
    report = build_patch_failure_report(
        failure_type,
        changed_files_attempted=list(attempted),
        changed_files_actual=list(actual),
        allowed_files=list(allowed),
        working_tree_clean=clean,
        chunk_number=1,
        failed_step="patch",
    )
    # model_copy to control the derived flags exactly and attach the id the
    # persisted enriched report would carry.
    return report.model_copy(update={
        "working_tree_clean": clean,
        "manual_intervention_needed": manual,
        "failure_report_id": failure_report_id,
    })


def _raw_files_expected(run_id, chunk_number=1) -> list[str]:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT files_expected FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()
    return json.loads(row[0])


def _pending(run_id, chunk_number=1):
    return [
        r for r in list_scope_expansion_requests_for_chunk(run_id, chunk_number)
        if r.status == ScopeExpansionStatus.PENDING.value
    ]


def _run_status(run_id) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :id"), {"id": run_id}
        ).fetchone()[0]


# 1 / 2 / 4
def test_eligible_scope_violation_creates_one_pending_request(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-1"),
    )

    assert result.created is True
    assert result.eligible is True
    pending = _pending(run_id)
    assert len(pending) == 1
    request = pending[0]
    assert request.run_id == run_id
    assert request.project_id == project_id
    assert request.chunk_number == 1
    assert request.failure_report_id == "frid-1"
    assert request.status == ScopeExpansionStatus.PENDING.value
    assert request.approved_files == []
    assert request.requested_files == ["src/extra.py"]


# 3
def test_requested_files_are_normalized_and_deduped(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1,
        _report(attempted=("src\\extra.py", "src/extra.py", "src/other.py")),
    )

    assert result.created is True
    # Backslash and forward-slash variants collapse to one normalized path.
    assert result.request.requested_files == ["src/extra.py", "src/other.py"]


# 5
def test_chunk_files_expected_unchanged_in_storage(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, files=("a.py", "b.py"))

    maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(attempted=("src/extra.py",)),
    )

    assert _raw_files_expected(run_id) == ["a.py", "b.py"]


# 6
def test_pending_request_does_not_affect_effective_scope(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, files=("a.py",))

    maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(attempted=("src/extra.py",)),
    )

    plan = get_chunk_plan_status(run_id)
    assert plan.chunks[0].files_expected == ["a.py"]


# 6b (#27F): the pending request is surfaced read-only on the chunk plan so the
# frontend can render approve/reject, without affecting effective scope.
def test_pending_request_is_surfaced_on_chunk_plan(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, files=("a.py",))

    created = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(attempted=("src/extra.py",), failure_report_id="frid-1"),
    )

    plan = get_chunk_plan_status(run_id)
    chunk = plan.chunks[0]
    # Read-only overlay: original scope is untouched.
    assert chunk.files_expected == ["a.py"]
    surfaced = chunk.pending_scope_expansion
    assert surfaced is not None
    assert surfaced.request_id == created.request.id
    assert surfaced.chunk_number == 1
    assert surfaced.failure_report_id == "frid-1"
    assert surfaced.requested_files == ["src/extra.py"]
    assert surfaced.status == ScopeExpansionStatus.PENDING.value


# 6c (#27F): non-pending (rejected/approved/superseded) requests are NOT surfaced
# as pending; only a live pending request drives the approve/reject UI.
def test_non_pending_request_is_not_surfaced_on_chunk_plan(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, files=("a.py",))

    created = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(attempted=("src/extra.py",), failure_report_id="frid-1"),
    )
    update_scope_expansion_request_status(
        created.request.id, ScopeExpansionStatus.REJECTED, decision_reason="no",
    )

    plan = get_chunk_plan_status(run_id)
    assert plan.chunks[0].pending_scope_expansion is None


# 7
def test_duplicate_same_failure_report_id_does_not_duplicate(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    first = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-1"),
    )
    second = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-1"),
    )

    assert first.created is True
    assert second.created is False
    assert second.reason == "already_pending_same_failure"
    assert second.request.id == first.request.id
    assert len(_pending(run_id)) == 1


# 8
def test_new_failure_report_id_supersedes_old_pending(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    first = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-1"),
    )
    second = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-2"),
    )

    assert second.created is True
    assert first.request.id in second.superseded_request_ids
    pending = _pending(run_id)
    assert len(pending) == 1
    assert pending[0].failure_report_id == "frid-2"
    # The old request is superseded, not deleted.
    all_requests = {r.id: r.status for r in list_scope_expansion_requests_for_chunk(run_id, 1)}
    assert all_requests[first.request.id] == ScopeExpansionStatus.SUPERSEDED.value


# 9
def test_non_scope_violation_creates_nothing(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1,
        _report(failure_type=PatchFailureType.TEST_FAILURE_AFTER_APPLY),
    )

    assert result.created is False
    assert result.eligible is False
    assert _pending(run_id) == []


# 10
def test_dirty_worktree_creates_nothing(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(clean=False),
    )

    assert result.created is False
    assert _pending(run_id) == []


# 11
def test_manual_intervention_creates_nothing(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(clean=True, manual=True),
    )

    assert result.created is False
    assert _pending(run_id) == []


# 12
def test_all_forbidden_extra_files_creates_nothing(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1,
        _report(attempted=(".env", "secrets/key.pem", ".github/workflows/ci.yml")),
    )

    assert result.created is False
    assert result.eligible is False
    assert _pending(run_id) == []


# 13 / 14
def test_in_force_amendment_blocks_further_creation(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    # First eligible failure -> pending -> approve it (now in force).
    first = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-1"),
    )
    update_scope_expansion_request_status(
        first.request.id, ScopeExpansionStatus.APPROVED, approved_files=["src/extra.py"],
    )
    assert count_in_force_scope_amendments(run_id, 1) == 1

    # A new eligible failure must NOT create another request (MAX = 1).
    blocked = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-2"),
    )
    assert blocked.created is False
    assert blocked.eligible is False
    assert _pending(run_id) == []

    # Same once the amendment is applied.
    update_scope_expansion_request_status(first.request.id, ScopeExpansionStatus.APPLIED)
    assert count_in_force_scope_amendments(run_id, 1) == 1
    blocked_applied = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id="frid-3"),
    )
    assert blocked_applied.created is False


# Missing failure_report_id cannot be tied to a request.
def test_missing_failure_report_id_creates_nothing(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    result = maybe_create_scope_expansion_request_for_failure(
        run_id, project_id, 1, _report(failure_report_id=None),
    )

    assert result.created is False
    assert result.reason == "missing_failure_report_id"
    assert _pending(run_id) == []


# Surfacing: run becomes awaiting_scope_approval; chunk stays failed.
def test_surfacing_sets_run_awaiting_scope_approval(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    chunked_orchestrator._surface_scope_expansion_if_eligible(
        run_id, 1, _report(failure_report_id="frid-1"),
    )

    assert _run_status(run_id) == "awaiting_scope_approval"
    assert len(_pending(run_id)) == 1


def test_surfacing_ineligible_leaves_run_unchanged(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs)

    # Force a known baseline run status, then surface a dirty (ineligible) failure.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET status = 'failed' WHERE id = :id"),
            {"id": run_id},
        )
    chunked_orchestrator._surface_scope_expansion_if_eligible(
        run_id, 1, _report(clean=False),
    )

    assert _run_status(run_id) == "failed"
    assert _pending(run_id) == []


# 15: #26 public retry eligibility still rejects SCOPE_VIOLATION.
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
