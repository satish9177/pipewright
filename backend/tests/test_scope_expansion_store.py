"""
test_scope_expansion_store.py
Tests for scope expansion persistence + the effective-scope overlay (#27C).

These tests prove:
  - scope_expansion_requests rows persist and read back.
  - get_chunk_plan_status overlays ONLY in-force (approved/applied) approved_files
    onto ChunkStatus.files_expected (the single merge site).
  - pending/rejected/superseded requests contribute nothing.
  - chunks.files_expected stays immutable in storage.
  - the effective scope reaches ChunkDefinition.files_expected via
    _definition_by_number, and does NOT come from the immutable triage JSON.

No routes, no retry execution, no orchestrator wiring are exercised.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.chunk_store import create_chunked_run, get_chunk_plan_status
from backend.pipeline.chunked_orchestrator import _definition_by_number
from backend.pipeline.scope_expansion import ScopeExpansionStatus
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
            conn.execute(
                text("DELETE FROM scope_expansion_requests WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM chunks WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


def _make_project(tmp_repo):
    return create_project(
        name=f"Scope Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _make_triage(run_id: str, project_id: str, files_by_chunk: dict[int, list[str]]) -> TriageResult:
    definitions = []
    for number in sorted(files_by_chunk):
        definitions.append(ChunkDefinition(
            chunk_number=number,
            title=f"Chunk {number}",
            description=f"Do chunk {number}",
            files_expected=list(files_by_chunk[number]),
            depends_on=[] if number == 1 else [number - 1],
            risk_level="low",
            token_estimate=100 * number,
            requires_human_review=False,
            rationale="ordered",
        ))
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Feature",
        complexity="easy" if len(files_by_chunk) == 1 else "medium",
        total_chunks=len(files_by_chunk),
        chunks=definitions,
        reasoning="split",
    )


def _seed_run(tmp_repo, tracked_runs, files_by_chunk: dict[int, list[str]]):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = _make_triage(run_id, project["id"], files_by_chunk)
    create_chunked_run(run_id, project["id"], "Feature", triage)
    return run_id, project["id"]


def _files_for_chunk(run_id: str, chunk_number: int) -> list[str]:
    plan = get_chunk_plan_status(run_id)
    for chunk in plan.chunks:
        if chunk.chunk_number == chunk_number:
            return chunk.files_expected
    raise AssertionError(f"chunk {chunk_number} not found")


def _raw_files_expected(run_id: str, chunk_number: int) -> list[str]:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT files_expected FROM chunks
            WHERE run_id = :run_id AND chunk_number = :chunk_number
        """), {"run_id": run_id, "chunk_number": chunk_number}).fetchone()
    return json.loads(row[0])


def _approve(request_id: str, approved_files: list[str]):
    return update_scope_expansion_request_status(
        request_id,
        ScopeExpansionStatus.APPROVED,
        approved_files=approved_files,
        decided_by="tester",
    )


# ---------------------------------------------------------------------------
# 1. Persist + read back
# ---------------------------------------------------------------------------


def test_request_persists_and_reads_back(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})

    created = create_scope_expansion_request(
        run_id, project_id, 1, "frid-1",
        requested_files=["src/extra.py"],
    )
    loaded = get_scope_expansion_request(created.id)

    assert loaded.id == created.id
    assert loaded.run_id == run_id
    assert loaded.chunk_number == 1
    assert loaded.failure_report_id == "frid-1"
    assert loaded.requested_files == ["src/extra.py"]
    assert loaded.approved_files == []
    assert loaded.status == ScopeExpansionStatus.PENDING.value
    assert loaded.created_at is not None


# ---------------------------------------------------------------------------
# 2-6. Only in-force (approved/applied) rows affect effective scope
# ---------------------------------------------------------------------------


def test_pending_request_does_not_affect_effective_scope(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    create_scope_expansion_request(
        run_id, project_id, 1, "frid", approved_files=["extra.py"],
    )

    assert _files_for_chunk(run_id, 1) == ["a.py"]


def test_approved_request_affects_effective_scope(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    _approve(request.id, ["extra.py"])

    assert _files_for_chunk(run_id, 1) == ["a.py", "extra.py"]


def test_applied_request_affects_effective_scope(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    _approve(request.id, ["extra.py"])
    update_scope_expansion_request_status(request.id, ScopeExpansionStatus.APPLIED)

    assert _files_for_chunk(run_id, 1) == ["a.py", "extra.py"]


def test_rejected_request_does_not_affect_effective_scope(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    update_scope_expansion_request_status(
        request.id, ScopeExpansionStatus.REJECTED, decision_reason="no",
    )

    assert _files_for_chunk(run_id, 1) == ["a.py"]


def test_superseded_request_does_not_affect_effective_scope(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    update_scope_expansion_request_status(request.id, ScopeExpansionStatus.SUPERSEDED)

    assert _files_for_chunk(run_id, 1) == ["a.py"]


# ---------------------------------------------------------------------------
# 7. Original chunks.files_expected stays immutable in storage
# ---------------------------------------------------------------------------


def test_original_chunk_files_expected_unchanged_in_storage(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py", "b.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    _approve(request.id, ["extra.py"])

    # Overlay shows the extra file...
    assert _files_for_chunk(run_id, 1) == ["a.py", "b.py", "extra.py"]
    # ...but the raw chunks column is untouched.
    assert _raw_files_expected(run_id, 1) == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# 8-9. Order preserved, extras appended, dedup
# ---------------------------------------------------------------------------


def test_effective_scope_preserves_order_and_appends_extras(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py", "b.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    _approve(request.id, ["c.py", "d.py"])

    assert _files_for_chunk(run_id, 1) == ["a.py", "b.py", "c.py", "d.py"]


def test_effective_scope_dedupes_duplicate_files(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py", "b.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    # "b.py" already in original; "c.py" repeated within approved.
    _approve(request.id, ["b.py", "c.py", "c.py"])

    assert _files_for_chunk(run_id, 1) == ["a.py", "b.py", "c.py"]


# ---------------------------------------------------------------------------
# 10. Multiple in-force rows: deterministic merge (documented v1 behavior)
# ---------------------------------------------------------------------------


def test_multiple_in_force_rows_merge_deterministically(tmp_repo, tracked_runs):
    """
    The store does not itself prevent multiple in-force rows for a chunk
    (MAX_SCOPE_AMENDMENTS is enforced at the route layer in a later slice). When
    more than one in-force row exists, the overlay merges them deterministically:
    original first, then each in-force row's approved_files ordered by created_at,
    deduplicated. This test pins that chosen behavior.
    """
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})

    first = create_scope_expansion_request(run_id, project_id, 1, "frid-1")
    _approve(first.id, ["c.py"])
    second = create_scope_expansion_request(run_id, project_id, 1, "frid-2")
    _approve(second.id, ["d.py"])

    # Pin created_at ordering so the merge order is deterministic regardless of
    # timestamp jitter on fast machines.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE scope_expansion_requests SET created_at = :ts WHERE id = :id"),
            {"ts": "2026-01-01T00:00:00+00:00", "id": first.id},
        )
        conn.execute(
            text("UPDATE scope_expansion_requests SET created_at = :ts WHERE id = :id"),
            {"ts": "2026-01-02T00:00:00+00:00", "id": second.id},
        )

    assert _files_for_chunk(run_id, 1) == ["a.py", "c.py", "d.py"]


# ---------------------------------------------------------------------------
# 11. Effective scope reaches ChunkDefinition via _definition_by_number,
#     and is NOT sourced from the immutable triage JSON.
# ---------------------------------------------------------------------------


def test_definition_by_number_receives_effective_scope_not_triage(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"], 2: ["x.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    _approve(request.id, ["extra.py"])

    plan = get_chunk_plan_status(run_id)
    definitions = _definition_by_number(plan)

    # The amended allowlist reaches the ChunkDefinition the executor/scope_guard see.
    assert definitions[1].files_expected == ["a.py", "extra.py"]
    # Crucially, it did NOT come from the immutable triage JSON: the triage chunk
    # still shows only the original scope. This pins the propagation-chain
    # invariant (§8) so a refactor that reads triage instead of ChunkStatus fails.
    triage_chunk = next(c for c in plan.triage.chunks if c.chunk_number == 1)
    assert triage_chunk.files_expected == ["a.py"]
    assert "extra.py" not in triage_chunk.files_expected
    # An unrelated chunk with no request is unaffected.
    assert definitions[2].files_expected == ["x.py"]


# ---------------------------------------------------------------------------
# Regression: zero in-force rows => byte-identical to pre-#27C output
# ---------------------------------------------------------------------------


def test_zero_requests_leaves_effective_scope_untouched(tmp_repo, tracked_runs):
    run_id, _ = _seed_run(tmp_repo, tracked_runs, {1: ["a.py", "b.py"]})

    assert _files_for_chunk(run_id, 1) == ["a.py", "b.py"]


def test_only_pending_requests_leave_effective_scope_untouched(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py", "b.py"]})
    create_scope_expansion_request(run_id, project_id, 1, "frid", approved_files=["z.py"])

    assert _files_for_chunk(run_id, 1) == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# Store helpers: listing + lifecycle enforcement
# ---------------------------------------------------------------------------


def test_list_requests_for_chunk_returns_oldest_first(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    first = create_scope_expansion_request(run_id, project_id, 1, "frid-1")
    second = create_scope_expansion_request(run_id, project_id, 1, "frid-2")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE scope_expansion_requests SET created_at = :ts WHERE id = :id"),
            {"ts": "2026-01-01T00:00:00+00:00", "id": first.id},
        )
        conn.execute(
            text("UPDATE scope_expansion_requests SET created_at = :ts WHERE id = :id"),
            {"ts": "2026-01-02T00:00:00+00:00", "id": second.id},
        )

    requests = list_scope_expansion_requests_for_chunk(run_id, 1)

    assert [r.id for r in requests] == [first.id, second.id]


def test_list_in_force_files_only_includes_approved_and_applied(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    approved = create_scope_expansion_request(run_id, project_id, 1, "frid-1")
    _approve(approved.id, ["c.py"])
    pending = create_scope_expansion_request(
        run_id, project_id, 1, "frid-2", approved_files=["never.py"],
    )

    in_force = list_in_force_scope_expansion_files(run_id, 1)

    assert in_force == ["c.py"]
    assert pending.status == ScopeExpansionStatus.PENDING.value


def test_update_status_rejects_illegal_transition(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")

    # pending -> applied is illegal (must pass through approved).
    with pytest.raises(ValueError):
        update_scope_expansion_request_status(request.id, ScopeExpansionStatus.APPLIED)


def test_update_status_rejects_transition_from_terminal_state(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    update_scope_expansion_request_status(request.id, ScopeExpansionStatus.REJECTED)

    with pytest.raises(ValueError):
        update_scope_expansion_request_status(request.id, ScopeExpansionStatus.APPROVED)


def test_apply_stamps_applied_at_and_keeps_approved_files(tmp_repo, tracked_runs):
    run_id, project_id = _seed_run(tmp_repo, tracked_runs, {1: ["a.py"]})
    request = create_scope_expansion_request(run_id, project_id, 1, "frid")
    _approve(request.id, ["c.py"])

    applied = update_scope_expansion_request_status(request.id, ScopeExpansionStatus.APPLIED)

    assert applied.status == ScopeExpansionStatus.APPLIED.value
    assert applied.applied_at is not None
    assert applied.approved_files == ["c.py"]
