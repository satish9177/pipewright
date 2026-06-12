"""
test_refine_completed_chunk.py
Phase 3 item 14 — post-success refinement: steering a COMPLETED, already
committed chunk into a new commit, with the cumulative final diff.

Pins the DECIDED behavior (PIPEWRIGHT_ITEM14_DESIGN.md, brief §14.3):

  - §0 invariant (the headline): a FAILED refinement of a committed chunk rolls
    the tree back to the chunk's commit and leaves chunk_status == "completed"
    with the original commit + committed summary intact — never "failed";
  - D1: a successful refinement re-pauses at the chunk gate (no commit yet); the
    new commit lands only on chunk re-approval (never an amend); final approval
    is re-required;
  - a no-op refinement makes no commit and does NOT mark the good chunk failed;
  - the stale final gate is superseded atomically when the run was awaiting final
    approval, and re-created after the refinement resolves — never two pending,
    never an orphan;
  - D3: the final-approval summary carries the cumulative branch diff, head+tail
    capped by policy.FINAL_DIFF_MAX_CHARS, never written to the turn log;
  - D2: refinements and failed-chunk retries/steers share one per-chunk budget,
    counted from the attempt ledger; auto/approval rows never count;
  - §5.3 re-confirm carries over; rejecting a refinement restores the completed
    chunk rather than destroying it.

Reuses the orchestrator suite's fake harness; no real AI, git mutation, or
GitHub.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.handoff import CoderHandoff
from backend.pipeline import chunked_orchestrator, policy
from backend.pipeline.chunk_attempt_store import (
    get_latest_completed_attempt_head,
    list_chunk_attempts,
    record_chunk_attempt,
)
from backend.pipeline.patch_failures import (
    REFINE_INELIGIBLE_CHUNK_NOT_COMPLETED,
    REFINE_INELIGIBLE_RUN_STATE,
    RETRY_INELIGIBLE_CAP_EXHAUSTED,
    RETRY_INELIGIBLE_DIRTY_WORKTREE,
    STEER_INELIGIBLE_CHUNK_STATE,
    PatchFailureType,
    count_human_attempt_ledger_rows,
    evaluate_completed_chunk_steer_eligibility,
)
from backend.pipeline.run_turn_store import list_run_turns
from backend.tests.test_chunked_orchestrator import (
    _seed_failed_chunk,
    create_run,
    patch_git_preflight,
    patch_retry_pipeline,
    reset_worktree_state,
    set_run_start_context,
)

pytestmark = pytest.mark.unit

STEER = "Use a clearer error message in the committed function."


# --- fixtures / harness ----------------------------------------------------- #


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "run_turns",
                "scope_expansion_requests",
                "chunk_attempts",
                "chunk_reviews",
                "approval_gates",
                "checkpoints",
                "chunks",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :r"), {"r": run_id}
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id}
            )


def _project_id(run_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT project_id FROM pipeline_runs WHERE id = :r"),
            {"r": run_id},
        ).fetchone()[0]


def _set_run_status(run_id: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status = :s, current_step = :s "
                "WHERE id = :r"
            ),
            {"s": status, "r": run_id},
        )


def _run_status(run_id: str) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :r"),
            {"r": run_id},
        ).fetchone()[0]


def _chunk_status(run_id: str, chunk_number: int = 1) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status FROM chunks WHERE run_id = :r "
                "AND chunk_number = :c"
            ),
            {"r": run_id, "c": chunk_number},
        ).fetchone()[0]


def _completion_summary(run_id: str, chunk_number: int = 1) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT completion_summary FROM chunks WHERE run_id = :r "
                "AND chunk_number = :c"
            ),
            {"r": run_id, "c": chunk_number},
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _pending_final_gates(run_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM approval_gates WHERE run_id = :r "
                "AND approval_type = 'final' AND status = 'pending'"
            ),
            {"r": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def _final_gates(run_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM approval_gates WHERE run_id = :r "
                "AND approval_type = 'final'"
            ),
            {"r": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def _seed_pending_final_gate(run_id: str, summary: str = "final pending") -> str:
    gate_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO approval_gates (
                    id, run_id, step, status, ai_summary, plain_english_summary,
                    risk_level, chunk_number, approval_type, created_at
                ) VALUES (
                    :id, :r, 'final-approval', 'pending', :s, :s, 'medium', 0,
                    'final', :now
                )
            """),
            {"id": gate_id, "r": run_id, "s": summary,
             "now": "2026-06-12T00:00:00+00:00"},
        )
    return gate_id


def _seed_approved_chunk_gate(run_id: str, chunk_number: int = 1) -> str:
    gate_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO approval_gates (
                    id, run_id, step, status, ai_summary, plain_english_summary,
                    risk_level, chunk_number, approval_type, created_at, decided_at
                ) VALUES (
                    :id, :r, 'chunk-approval', 'approved', 's', 's', 'high', :c,
                    'chunk', :now, :now
                )
            """),
            {"id": gate_id, "r": run_id, "c": chunk_number,
             "now": "2026-06-12T00:00:00+00:00"},
        )
    return gate_id


def _seed_completed_chunk(
    run_id: str,
    chunk_number: int = 1,
    *,
    head_sha: str = "commit-sha-1",
    run_status: str = "awaiting_final_approval",
    with_approved_gate: bool = False,
    seed_final_gate: bool = True,
    human_attempt_rows: int = 0,
) -> None:
    """
    Model a chunk that already executed, committed, and completed, plus the run
    state at the moment a human goes to refine it. Records a completed ledger row
    (the chunk's commit head), optionally an approved chunk gate (a human-reviewed
    chunk), and prior human-attempt ledger rows (for D2 budget tests).
    """
    project_id = _project_id(run_id)
    chunked_orchestrator.save_chunk_completion_summary(
        run_id,
        chunk_number,
        {
            "summary": f"chunk {chunk_number} committed and complete",
            "chunk_description": f"do chunk {chunk_number}",
            "files_modified": [f"modified_{chunk_number}.py"],
        },
    )
    chunked_orchestrator.update_chunk_status(run_id, chunk_number, "completed")
    record_chunk_attempt(
        run_id=run_id,
        project_id=project_id,
        chunk_number=chunk_number,
        entry_mode="fresh",
        final_outcome_class="SUCCESS",
        final_status="completed",
        head_sha=head_sha,
    )
    for _ in range(human_attempt_rows):
        record_chunk_attempt(
            run_id=run_id,
            project_id=project_id,
            chunk_number=chunk_number,
            entry_mode="steered",
            final_outcome_class="CODE_REJECTED",
            final_status="failed",
            head_sha=head_sha,
        )
    _set_run_status(run_id, run_status)
    if with_approved_gate:
        _seed_approved_chunk_gate(run_id, chunk_number)
    if run_status == "awaiting_final_approval" and seed_final_gate:
        _seed_pending_final_gate(run_id)


def _patch_refine_git(monkeypatch, run_id: str, *, head_sha: str = "head-sha"):
    """Patch the read-only git seams a refinement touches (tmp_repo is no git)."""
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "get_current_hash", lambda repo: head_sha
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "show_commit",
        lambda sha, repo: f"--- diff for commit {sha} ---\n+changed line\n",
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "diff_range",
        lambda base, repo, head_ref="HEAD": (
            f"--- cumulative diff {base}..{head_ref} ---\n+committed change\n"
        ),
    )
    # The driver's review stage is advisory; keep it offline + deterministic.
    async def _no_review(**_kwargs):
        return None

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", _no_review)


def _spy_rollback(monkeypatch) -> list:
    rollbacks = []

    def fake_rollback(run_id_arg, chunk_number=0):
        rollbacks.append((run_id_arg, chunk_number))
        reset_worktree_state()
        return True

    monkeypatch.setattr(chunked_orchestrator, "rollback_patch", fake_rollback)
    return rollbacks


def _forbid_planner(monkeypatch):
    async def boom_planner(*_a, **_k):
        raise AssertionError("a refinement must not run the planner")

    monkeypatch.setattr(chunked_orchestrator, "run_planner", boom_planner)


def _forbid_execution(monkeypatch):
    async def _boom_coder(*_a, **_k):
        raise AssertionError("an ineligible refinement must not call the coder")

    def _boom(*_a, **_k):
        raise AssertionError("an ineligible refinement must not run apply/test")

    monkeypatch.setattr(chunked_orchestrator, "run_coder", _boom_coder)
    monkeypatch.setattr(chunked_orchestrator, "dry_run_changes", _boom)
    monkeypatch.setattr(chunked_orchestrator, "apply_patch_guarded", _boom)
    monkeypatch.setattr(chunked_orchestrator, "run_tests", _boom)


# --- §0 invariant: a failed refinement never degrades the completed chunk --- #


@pytest.mark.asyncio
async def test_failing_refinement_preserves_completed_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_completed_chunk(run_id, 1, run_status="chunk_approved")
    prior_summary = _completion_summary(run_id, 1)

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=False, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _spy_rollback(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    # The headline: the good chunk and its commit survive; never marked failed.
    assert result["status"] == "refinement_failed"
    assert result["chunk_status"] == "completed"
    assert _chunk_status(run_id, 1) == "completed"
    # Committed summary untouched (not overwritten by a failure report).
    assert _completion_summary(run_id, 1) == prior_summary
    # No commit happened.
    assert not any(c[0] == "commit" for c in calls)
    # Exactly one steered/failed attempt recorded + one turn row.
    steered = [
        a for a in list_chunk_attempts(run_id, 1)
        if a["entry_mode"] == "steered"
    ]
    assert len(steered) == 1
    assert steered[0]["final_status"] == "failed"
    assert len(list_run_turns(run_id, 1)) == 1
    # Entry-a run state restored.
    assert _run_status(run_id) == "chunk_approved"


@pytest.mark.asyncio
async def test_failing_refinement_entry_b_reopens_final_gate(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(run_id, 1, run_status="awaiting_final_approval")
    assert len(_pending_final_gates(run_id)) == 1

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=False, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _spy_rollback(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    assert result["status"] == "refinement_failed"
    assert _chunk_status(run_id, 1) == "completed"
    # The stale final gate was superseded and a fresh one re-created: exactly one
    # pending final gate, never two, never an orphan.
    assert len(_pending_final_gates(run_id)) == 1
    statuses = sorted(g["status"] for g in _final_gates(run_id))
    assert statuses == ["pending", "superseded"]
    assert _run_status(run_id) == "awaiting_final_approval"


# --- D1: successful refinement re-pauses, new commit on re-approval ---------- #


@pytest.mark.asyncio
async def test_successful_refinement_pauses_then_commits_on_approval(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(
        run_id, 1, run_status="awaiting_final_approval", with_approved_gate=True
    )

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=True, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    # D1: re-pause at the chunk gate, no commit yet.
    assert result["status"] == "awaiting_chunk_approval"
    assert not any(c[0] == "commit" for c in calls)
    assert _chunk_status(run_id, 1) == "awaiting_chunk_approval"
    # The stale final gate was superseded while the refinement is in flight.
    assert len(_pending_final_gates(run_id)) == 0

    # Re-approve → exactly one NEW commit, chunk completed, final re-required.
    approve = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)
    commits = [c for c in calls if c[0] == "commit"]
    assert len(commits) == 1
    assert _chunk_status(run_id, 1) == "completed"
    assert approve["status"] == "awaiting_final_approval"
    assert len(_pending_final_gates(run_id)) == 1


# --- no-op refinement: no commit, chunk stays completed (not failed) -------- #


@pytest.mark.asyncio
async def test_noop_refinement_approve_time_interception(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(run_id, 1, run_status="chunk_approved")
    prior_summary = _completion_summary(run_id, 1)

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=True, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)
    assert result["status"] == "awaiting_chunk_approval"

    # Model a byte-identical regeneration: the tree reads clean at approve time.
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: True
    )
    approve = chunked_orchestrator.approve_chunk_and_commit(run_id, 1)

    # No commit, chunk stays completed (NOT failed), original summary restored.
    assert approve["status"] == "refinement_no_op"
    assert not any(c[0] == "commit" for c in calls)
    assert _chunk_status(run_id, 1) == "completed"
    assert _completion_summary(run_id, 1) == prior_summary


@pytest.mark.asyncio
async def test_empty_coder_refinement_keeps_chunk_completed(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_completed_chunk(run_id, 1, run_status="chunk_approved")
    prior_summary = _completion_summary(run_id, 1)

    empty = CoderHandoff(
        run_id=run_id,
        feature_description="enriched",
        files_changed=[],
        summary="No change needed",
        suggested_memory_entries=[],
    )
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, coder_result=empty, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _spy_rollback(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    # Driver-side NO_CHANGES routes through the §0 finalizer: completed, not failed.
    assert result["status"] == "refinement_failed"
    assert _chunk_status(run_id, 1) == "completed"
    assert _completion_summary(run_id, 1) == prior_summary
    assert not any(c[0] == "commit" for c in calls)


# --- reject a refinement: restore the completed chunk, don't destroy it ------ #


@pytest.mark.asyncio
async def test_reject_refinement_restores_completed_chunk(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(run_id, 1, run_status="chunk_approved")
    prior_summary = _completion_summary(run_id, 1)

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=True, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _spy_rollback(monkeypatch)
    _forbid_planner(monkeypatch)

    await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)
    assert _chunk_status(run_id, 1) == "awaiting_chunk_approval"

    reject = chunked_orchestrator.reject_chunk_and_rollback(run_id, 1, "not quite")

    # The refinement is rejected but the committed chunk survives, run restored.
    assert reject["status"] == "refinement_rejected"
    assert reject["chunk_status"] == "completed"
    assert _chunk_status(run_id, 1) == "completed"
    assert _completion_summary(run_id, 1) == prior_summary
    assert _run_status(run_id) == "chunk_approved"


# --- re-open final approval: gate lifecycle, both entry states -------------- #


@pytest.mark.asyncio
async def test_refinement_supersede_blocks_final_approval_until_resolved(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(run_id, 1, run_status="awaiting_final_approval")

    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=True, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_planner(monkeypatch)

    await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)
    # While the refinement is paused, final approval is blocked (no pending gate).
    assert len(_pending_final_gates(run_id)) == 0
    client = TestClient(app)
    blocked = client.post(f"/runs/{run_id}/final-approval/approve")
    assert blocked.status_code == 404

    # Resolving the refinement (re-approve) re-creates exactly one final gate.
    chunked_orchestrator.approve_chunk_and_commit(run_id, 1)
    assert len(_pending_final_gates(run_id)) == 1


# --- D3: cumulative diff in the final-approval summary ---------------------- #


def test_cumulative_diff_in_final_summary_capped_and_not_persisted(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(
        run_id, 1, run_status="chunk_approved", seed_final_gate=False
    )

    big_diff = "+" + ("x" * (policy.FINAL_DIFF_MAX_CHARS * 3))
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "diff_range",
        lambda base, repo, head_ref="HEAD": big_diff,
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "ensure_clean_worktree", lambda repo: None
    )

    plan_status = chunked_orchestrator.get_chunk_plan_status(run_id)
    summary = chunked_orchestrator._build_final_approval_summary(
        run_id, plan_status, f"pipewright/{run_id[:8]}", str(tmp_repo)
    )

    assert "Cumulative diff (branch vs. base):" in summary
    assert "truncated" in summary
    # The cap is read from policy, not a buried literal: a smaller cap truncates
    # more aggressively.
    assert len(summary) < len(big_diff)
    # The diff is display-only: it is never written to the append-only turn log.
    assert list_run_turns(run_id) == []


def test_final_summary_diff_unavailable_degrades_not_blocks(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    set_run_start_context(run_id)
    _seed_completed_chunk(
        run_id, 1, run_status="chunk_approved", seed_final_gate=False
    )

    def _boom_diff(base, repo, head_ref="HEAD"):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(chunked_orchestrator.local_git, "diff_range", _boom_diff)

    plan_status = chunked_orchestrator.get_chunk_plan_status(run_id)
    summary = chunked_orchestrator._build_final_approval_summary(
        run_id, plan_status, f"pipewright/{run_id[:8]}", str(tmp_repo)
    )
    assert "[cumulative diff unavailable" in summary


# --- D2: shared per-chunk budget from the ledger ---------------------------- #


def test_count_human_attempt_ledger_rows_counts_only_human_non_completed():
    rows = [
        {"entry_mode": "fresh", "final_status": "completed"},
        {"entry_mode": "human_retry", "final_status": "failed"},
        {"entry_mode": "steered", "final_status": "awaiting_chunk_approval"},
        {"entry_mode": "steered", "final_status": "completed"},  # approval row
        {"entry_mode": "human_retry", "final_status": "completed"},  # approval row
        {"entry_mode": "auto_retry", "final_status": "failed"},
    ]
    # Only the failed human_retry and the awaiting_chunk_approval steered count.
    assert count_human_attempt_ledger_rows(rows) == 2


@pytest.mark.asyncio
async def test_shared_budget_exhaustion_refuses_with_terminal_narrative(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    # The chunk already spent its whole human-attempt budget (e.g. via failed
    # retries before it eventually completed).
    _seed_completed_chunk(
        run_id,
        1,
        run_status="chunk_approved",
        human_attempt_rows=policy.HUMAN_ATTEMPT_BUDGET,
    )
    patch_git_preflight(monkeypatch, run_id=run_id)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_execution(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == RETRY_INELIGIBLE_CAP_EXHAUSTED
    # Nothing mutated.
    assert _chunk_status(run_id, 1) == "completed"
    assert list_run_turns(run_id, 1) == []


# --- §5.3 conservative re-confirm carries over ------------------------------ #


@pytest.mark.asyncio
async def test_out_of_scope_refinement_requires_reconfirm(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_completed_chunk(run_id, 1, run_status="chunk_approved")
    patch_git_preflight(monkeypatch, run_id=run_id)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_execution(monkeypatch)
    _forbid_planner(monkeypatch)

    steer = "Only modify: totally_other_file.py to fix the message."
    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, steer)

    assert result["status"] == "steer_needs_scope_confirmation"
    assert result["status_code"] == 409
    # Zero mutation, budget unspent.
    assert _chunk_status(run_id, 1) == "completed"
    assert list_run_turns(run_id, 1) == []
    steered = [
        a for a in list_chunk_attempts(run_id, 1) if a["entry_mode"] == "steered"
    ]
    assert steered == []


# --- eligibility / dispatch ------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_chunk_routes_to_item13_path(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    frid = _seed_failed_chunk(
        run_id, 1, failure_type=PatchFailureType.TEST_REGRESSION
    )
    _set_run_status(run_id, "failed")
    calls = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, tests_ok=True, calls=calls)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, frid, STEER)

    # The failed chunk still goes through the item-13 recovered-patch pause.
    assert result["status"] == "awaiting_chunk_approval"
    summary = _completion_summary(run_id, 1)
    assert summary["kind"] == "recovered_patch_review"
    assert summary.get("refinement") is None


@pytest.mark.asyncio
async def test_completed_chunk_in_failed_run_is_refused(
    monkeypatch, tmp_repo, tracked_runs
):
    # Chunk 1 completed, chunk 2 failed → run is failed on a later chunk. Refining
    # chunk 1 must be refused (would clobber chunk 2's failed-run blocker).
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    _seed_completed_chunk(
        run_id, 1, run_status="awaiting_final_approval", seed_final_gate=False
    )
    _seed_failed_chunk(run_id, 2)
    _set_run_status(run_id, "failed")
    patch_git_preflight(monkeypatch, run_id=run_id)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_execution(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == REFINE_INELIGIBLE_RUN_STATE
    # Zero mutation: chunk 1 still completed, chunk 2 still failed.
    assert _chunk_status(run_id, 1) == "completed"
    assert _chunk_status(run_id, 2) == "failed"
    assert list_run_turns(run_id) == []


@pytest.mark.asyncio
async def test_pending_chunk_is_not_steerable(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    # chunk 1 is freshly created → status "pending".
    patch_git_preflight(monkeypatch, run_id=run_id)
    _forbid_execution(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)

    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 422
    assert result["reason"] == STEER_INELIGIBLE_CHUNK_STATE


@pytest.mark.asyncio
async def test_completed_chunk_after_final_approved_is_refused(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_completed_chunk(
        run_id, 1, run_status="final_approved", seed_final_gate=False
    )
    patch_git_preflight(monkeypatch, run_id=run_id)
    _patch_refine_git(monkeypatch, run_id)
    _forbid_execution(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)
    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == REFINE_INELIGIBLE_RUN_STATE


@pytest.mark.asyncio
async def test_dirty_tree_refuses_refinement(
    monkeypatch, tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _seed_completed_chunk(run_id, 1, run_status="chunk_approved")
    patch_git_preflight(monkeypatch, run_id=run_id)
    _patch_refine_git(monkeypatch, run_id)
    monkeypatch.setattr(
        chunked_orchestrator.local_git, "is_working_tree_clean", lambda repo: False
    )
    _forbid_execution(monkeypatch)
    _forbid_planner(monkeypatch)

    result = await chunked_orchestrator.steer_chunk(run_id, 1, None, STEER)
    assert result["status"] == "retry_ineligible"
    assert result["status_code"] == 409
    assert result["reason"] == RETRY_INELIGIBLE_DIRTY_WORKTREE


# --- §8 cross-seam: resume HEAD lookup is recency-ordered ------------------- #


def test_latest_completed_attempt_head_is_recency_ordered(
    tmp_repo, tracked_runs
):
    run_id, _project = create_run(tmp_repo, tracked_runs, chunks=2)
    project_id = _project["id"]

    # Chunk 2 completed first; chunk 1 was refined LATER (a lower-numbered chunk
    # committed after a higher one). In production these are seconds/minutes
    # apart (the human reads the result, then steers); set explicit created_at so
    # the recency ordering is exercised deterministically (Windows clock
    # granularity can collapse two same-instant inserts).
    def _insert(chunk_number, head_sha, created_at, attempt_number):
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO chunk_attempts (
                        id, run_id, project_id, chunk_number, attempt_number,
                        entry_mode, stage_outcomes_json, evidence_refs_json,
                        final_outcome_class, final_status, head_sha, created_at
                    ) VALUES (
                        :id, :r, :p, :cn, :an, :em, '[]', '[]', 'SUCCESS',
                        'completed', :sha, :created_at
                    )
                """),
                {
                    "id": str(uuid.uuid4()), "r": run_id, "p": project_id,
                    "cn": chunk_number, "an": attempt_number, "em": "fresh"
                    if chunk_number == 2 else "steered",
                    "sha": head_sha, "created_at": created_at,
                },
            )

    _insert(2, "sha-chunk2", "2026-06-12T00:00:01+00:00", 1)
    _insert(1, "sha-refined-chunk1", "2026-06-12T00:05:00+00:00", 2)

    latest = get_latest_completed_attempt_head(run_id)
    assert latest is not None
    # Recency, not highest chunk number: the refinement of chunk 1 is HEAD.
    assert latest["head_sha"] == "sha-refined-chunk1"
    assert latest["chunk_number"] == 1


# --- pure evaluator ---------------------------------------------------------- #


def test_completed_chunk_eligibility_gate_order():
    # not completed → 422
    d = evaluate_completed_chunk_steer_eligibility(
        chunk_status="failed", run_status="chunk_approved",
        dependencies_met=True, working_tree_clean=True, human_attempts_used=0,
    )
    assert not d.eligible and d.status_code == 422
    assert d.reason == REFINE_INELIGIBLE_CHUNK_NOT_COMPLETED

    # completed but run not refinable → 409
    d = evaluate_completed_chunk_steer_eligibility(
        chunk_status="completed", run_status="final_approved",
        dependencies_met=True, working_tree_clean=True, human_attempts_used=0,
    )
    assert not d.eligible and d.status_code == 409
    assert d.reason == REFINE_INELIGIBLE_RUN_STATE

    # dirty tree → 409
    d = evaluate_completed_chunk_steer_eligibility(
        chunk_status="completed", run_status="awaiting_final_approval",
        dependencies_met=True, working_tree_clean=False, human_attempts_used=0,
    )
    assert not d.eligible and d.status_code == 409
    assert d.reason == RETRY_INELIGIBLE_DIRTY_WORKTREE

    # budget exhausted → 422
    d = evaluate_completed_chunk_steer_eligibility(
        chunk_status="completed", run_status="chunk_approved",
        dependencies_met=True, working_tree_clean=True,
        human_attempts_used=policy.HUMAN_ATTEMPT_BUDGET,
    )
    assert not d.eligible and d.status_code == 422
    assert d.reason == RETRY_INELIGIBLE_CAP_EXHAUSTED

    # all gates pass → eligible
    d = evaluate_completed_chunk_steer_eligibility(
        chunk_status="completed", run_status="awaiting_final_approval",
        dependencies_met=True, working_tree_clean=True, human_attempts_used=0,
    )
    assert d.eligible and d.status_code is None
