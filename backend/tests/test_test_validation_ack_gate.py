"""
test_test_validation_ack_gate.py
Tests for the final-approval weak/no-test acknowledgement gate (#28F).

Covers the acknowledgement store/route and the final-approval precondition,
including the diff-hash binding that makes a stale acknowledgement (after a
retry/amendment) re-require a fresh one. Pure backend; no git, no LLM, no real
test execution — verdicts and diff hashes are seeded directly.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import backend.git.local_git as local_git
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.approval_gate import create_final_approval_gate
from backend.pipeline.chunk_store import (
    create_chunked_run,
    save_chunk_test_run_verdict,
    update_chunk_status,
)
from backend.pipeline.test_validation_ack_store import (
    FINAL_APPROVAL_ACK_ELIGIBLE,
    FINAL_APPROVAL_ACK_REQUIRED,
    ChunkAckRequirement,
    evaluate_final_approval_ack_eligibility,
)
from backend.pipeline.test_run_validation import classify_test_run
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit

client = TestClient(app)


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "test_validation_acknowledgements",
                "checkpoints",
                "approval_gates",
                "chunks",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


# Commands whose classify_test_run verdict we rely on.
_VERDICT_COMMANDS = {
    "weak": ("python --version", 0, "Python 3.11.5"),
    "none": ("", 0, ""),
    "strong": ("pytest", 0, "===== 5 passed in 0.10s ====="),
    "unknown": ("./scripts/check.sh", 0, "running checks..."),
}


def _make_run(tmp_path, tracked_runs, *, chunk_count=1):
    project = create_project(
        name=f"ack-{uuid.uuid4()}",
        repo_path=str(tmp_path),
        test_command="pytest",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    chunks = [
        ChunkDefinition(
            chunk_number=n,
            title=f"Chunk {n}",
            description="do it",
            files_expected=["a.py"],
            depends_on=[] if n == 1 else [n - 1],
            risk_level="low",
            token_estimate=10,
            requires_human_review=False,
            rationale="r",
        )
        for n in range(1, chunk_count + 1)
    ]
    triage = TriageResult(
        run_id=run_id,
        project_id=project["id"],
        feature_description="x",
        complexity="easy" if chunk_count == 1 else "medium",
        total_chunks=chunk_count,
        chunks=chunks,
        reasoning="r",
    )
    create_chunked_run(run_id, project["id"], "x", triage)
    return run_id, project


def _seed_verdict(run_id, chunk_number, verdict_key):
    command, exit_code, output = _VERDICT_COMMANDS[verdict_key]
    result = classify_test_run(command, exit_code, output)
    assert result.verdict.value == verdict_key  # guard the fixture intent
    save_chunk_test_run_verdict(run_id, chunk_number, result)
    update_chunk_status(run_id, chunk_number, "completed")


def _seed_diff_hash(run_id, chunk_number, git_hash):
    # The chunk's latest test checkpoint git hash is the canonical diff identity.
    save_checkpoint(
        run_id=run_id,
        step="test",
        output={"passed": True},
        handoff_contract={"passed": True},
        git_hash=git_hash,
        tests_passed=True,
        chunk_number=chunk_number,
    )


def _ack(run_id, chunk_number, reason="manually verified"):
    return client.post(
        f"/runs/{run_id}/chunks/{chunk_number}/test-validation/acknowledge",
        json={"reason": reason},
    )


def _approve_final(run_id):
    return client.post(f"/runs/{run_id}/final-approval/approve")


def _run_status(run_id) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM pipeline_runs WHERE id = :id"),
            {"id": run_id},
        ).fetchone()
    return row[0] if row else ""


# ==========================================================================
# Acknowledgement route/store
# ==========================================================================


def test_can_acknowledge_weak_verdict(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")

    response = _ack(run_id, 1)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "test_validation_acknowledged"
    assert body["verdict"] == "weak"
    assert body["acknowledged_diff_hash"] == "HASH_A"


def test_can_acknowledge_none_verdict(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "none")
    _seed_diff_hash(run_id, 1, "HASH_A")

    response = _ack(run_id, 1)
    assert response.status_code == 200
    assert response.json()["verdict"] == "none"


def test_strong_verdict_cannot_be_acknowledged(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "strong")
    _seed_diff_hash(run_id, 1, "HASH_A")

    response = _ack(run_id, 1)
    assert response.status_code == 409


def test_unknown_verdict_cannot_be_acknowledged(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "unknown")
    _seed_diff_hash(run_id, 1, "HASH_A")

    response = _ack(run_id, 1)
    assert response.status_code == 409


def test_no_verdict_cannot_be_acknowledged(tmp_path, tracked_runs):
    # Chosen v1 policy: a chunk with no recorded verdict does not require (or
    # permit) acknowledgement, so legacy/skip-completed runs are never wedged.
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH_A")  # checkpoint but no verdict persisted

    response = _ack(run_id, 1)
    assert response.status_code == 409


def test_acknowledgement_stores_full_audit_row(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")

    _ack(run_id, 1, reason="small change, manually verified")

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT run_id, chunk_number, verdict, acknowledged_diff_hash,
                   reason, acknowledged_at, status
            FROM test_validation_acknowledgements
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
    data = dict(row._mapping)
    assert data["run_id"] == run_id
    assert data["chunk_number"] == 1
    assert data["verdict"] == "weak"
    assert data["acknowledged_diff_hash"] == "HASH_A"
    assert data["reason"] == "small change, manually verified"
    assert data["acknowledged_at"] is not None
    assert data["status"] == "active"


def test_reacknowledging_same_diff_is_idempotent(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")

    first = _ack(run_id, 1)
    second = _ack(run_id, 1)
    assert first.status_code == 200
    assert second.status_code == 200

    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM test_validation_acknowledgements
            WHERE run_id = :run_id AND chunk_number = 1 AND status = 'active'
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 1  # no duplicate row


def test_wrong_chunk_returns_404_and_mutates_nothing(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")

    response = _ack(run_id, 999)
    assert response.status_code == 404
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM test_validation_acknowledgements
            WHERE run_id = :run_id
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 0


def test_acknowledge_without_diff_hash_is_409(tmp_path, tracked_runs):
    # Weak verdict but no test checkpoint -> indeterminate diff identity -> refuse
    # to record an unbound acknowledgement.
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")  # no _seed_diff_hash

    response = _ack(run_id, 1)
    assert response.status_code == 409
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM test_validation_acknowledgements
            WHERE run_id = :run_id
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 0


# ==========================================================================
# Final approval guard
# ==========================================================================


def test_final_approval_ack_decision_allows_when_nothing_blocks():
    decision = evaluate_final_approval_ack_eligibility([])
    assert decision.eligible is True
    assert decision.reason == FINAL_APPROVAL_ACK_ELIGIBLE
    assert decision.status_code is None
    assert decision.blocked_requirements == ()


def test_final_approval_ack_decision_blocks_missing_or_stale_requirements():
    missing = ChunkAckRequirement(
        chunk_number=1,
        verdict="weak",
        state="missing",
        current_diff_hash="HASH_A",
    )
    stale = ChunkAckRequirement(
        chunk_number=2,
        verdict="none",
        state="stale",
        current_diff_hash="HASH_B",
    )
    decision = evaluate_final_approval_ack_eligibility([missing, stale])
    assert decision.eligible is False
    assert decision.reason == FINAL_APPROVAL_ACK_REQUIRED
    assert decision.status_code == 409
    assert decision.blocked_requirements == (missing, stale)


def test_weak_without_acknowledgement_blocks_final_approval(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    response = _approve_final(run_id)
    assert response.status_code == 409
    assert _run_status(run_id) != "final_approved"


def test_none_without_acknowledgement_blocks_final_approval(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "none")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    response = _approve_final(run_id)
    assert response.status_code == 409
    assert _run_status(run_id) != "final_approved"


def test_weak_with_matching_acknowledgement_allows_final_approval(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    assert _ack(run_id, 1).status_code == 200
    response = _approve_final(run_id)
    assert response.status_code == 200
    assert response.json()["status"] == "final_approved"
    assert _run_status(run_id) == "final_approved"


def test_none_with_matching_acknowledgement_allows_final_approval(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "none")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    assert _ack(run_id, 1).status_code == 200
    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_strong_verdict_allows_final_approval_without_acknowledgement(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "strong")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    response = _approve_final(run_id)
    assert response.status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_unknown_verdict_allows_final_approval_without_acknowledgement(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "unknown")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    response = _approve_final(run_id)
    assert response.status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_no_verdict_allows_final_approval(tmp_path, tracked_runs):
    # Legacy/skip-completed run: no verdict recorded -> no acknowledgement needed.
    run_id, _ = _make_run(tmp_path, tracked_runs)
    update_chunk_status(run_id, 1, "completed")
    create_final_approval_gate(run_id, "final summary")

    response = _approve_final(run_id)
    assert response.status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_stale_acknowledgement_blocks_final_approval(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    assert _ack(run_id, 1).status_code == 200  # acked against HASH_A
    # A retry/amendment changes the diff: a new test checkpoint with a new hash.
    _seed_diff_hash(run_id, 1, "HASH_B")

    response = _approve_final(run_id)
    assert response.status_code == 409  # old ack is now stale
    assert _run_status(run_id) != "final_approved"


def test_retry_changed_diff_requires_fresh_acknowledgement(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    assert _ack(run_id, 1).status_code == 200
    _seed_diff_hash(run_id, 1, "HASH_B")  # retry changed the diff
    assert _approve_final(run_id).status_code == 409

    # Acknowledging the NEW diff unblocks final approval.
    assert _ack(run_id, 1).status_code == 200
    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_guard_blocks_before_commit_and_final_mutation(
    tmp_path, tracked_runs, monkeypatch
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    commit_called = {"value": False}
    monkeypatch.setattr(
        local_git,
        "commit_files",
        lambda *a, **k: commit_called.__setitem__("value", True) or "hash",
    )

    response = _approve_final(run_id)

    assert response.status_code == 409
    assert commit_called["value"] is False  # no commit on the blocked path
    assert _run_status(run_id) != "final_approved"  # no final-approved mutation
    with engine.connect() as conn:
        gate_status = conn.execute(text("""
            SELECT status FROM approval_gates
            WHERE run_id = :run_id AND approval_type = 'final'
        """), {"run_id": run_id}).fetchone()[0]
    assert gate_status == "pending"  # gate untouched


def test_multi_chunk_blocks_until_every_weak_chunk_acknowledged(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs, chunk_count=2)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_1")
    _seed_verdict(run_id, 2, "weak")
    _seed_diff_hash(run_id, 2, "HASH_2")
    create_final_approval_gate(run_id, "final summary")

    # Acknowledge only chunk 1 -> still blocked by chunk 2.
    assert _ack(run_id, 1).status_code == 200
    assert _approve_final(run_id).status_code == 409
    assert _run_status(run_id) != "final_approved"

    # Acknowledge chunk 2 -> unblocked.
    assert _ack(run_id, 2).status_code == 200
    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_final_approval_without_pending_gate_is_404_not_409(tmp_path, tracked_runs):
    # A weak verdict but no pending final gate must keep the existing 404 (no
    # gate), not be masked by the acknowledgement 409. The guard defers to the
    # existing behavior when no final gate is pending.
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")

    response = _approve_final(run_id)
    assert response.status_code == 404
