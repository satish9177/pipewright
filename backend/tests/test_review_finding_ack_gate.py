"""
test_review_finding_ack_gate.py
Phase 4 item 15: reviewer informed-approval soft gate.

These tests pin the headline safety invariant: reviewer ack can require a human
glance only for a current delivered high x A1 finding, and it must never become
a denial-of-approval vector. Missing, unavailable, failed, stale, non-A1, and
selector-error cases all fail open to ordinary approval.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import backend.routes.chunks as chunks_route
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import engine, init_db
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy
from backend.pipeline.approval_gate import create_final_approval_gate
from backend.pipeline.chunk_review_store import create_review
from backend.pipeline.chunk_store import (
    create_chunked_run,
    save_chunk_test_run_verdict,
    update_chunk_status,
)
from backend.pipeline.review_finding_ack_store import (
    REVIEW_ACK_MISSING,
    REVIEW_FINDING_ACK_ELIGIBLE,
    REVIEW_FINDING_ACK_REQUIRED,
    ReviewAckRequirement,
    evaluate_review_ack_eligibility,
    finding_to_steer_text,
    review_findings_requiring_ack,
)
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ChunkReviewStatus,
    ChunkReviewVerdict,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
)
from backend.pipeline.test_run_validation import classify_test_run
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit

client = TestClient(app)


@pytest.fixture()
def tracked_runs():
    init_db()
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "review_finding_acknowledgements",
                "test_validation_acknowledgements",
                "chunk_reviews",
                "checkpoints",
                "approval_gates",
                "run_turns",
                "chunk_attempts",
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


def _make_run(tmp_path, tracked_runs, *, chunk_count=1):
    project = create_project(
        name=f"review-ack-{uuid.uuid4()}",
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


def _seed_diff_hash(run_id, chunk_number, git_hash):
    save_checkpoint(
        run_id=run_id,
        step="test",
        output={"passed": True},
        handoff_contract={"passed": True},
        git_hash=git_hash,
        tests_passed=True,
        chunk_number=chunk_number,
    )


def _seed_verdict(run_id, chunk_number, command, exit_code, output):
    result = classify_test_run(command, exit_code, output)
    save_chunk_test_run_verdict(run_id, chunk_number, result)


def _seed_strong_verdict(run_id, chunk_number):
    _seed_verdict(run_id, chunk_number, "pytest", 0, "===== 5 passed in 0.10s =====")


def _seed_weak_verdict(run_id, chunk_number):
    _seed_verdict(run_id, chunk_number, "python --version", 0, "Python 3.11.5")


def _finding(
    *,
    category=ReviewFindingCategory.REQUIREMENT_MISMATCH,
    severity=ReviewFindingSeverity.HIGH,
):
    return ReviewFinding(
        category=category,
        severity=severity,
        title="Requirement not met",
        explanation="The implementation does not meet the requested behavior.",
        affected_files=["a.py"],
        suggested_human_check="Check the requested behavior against a.py.",
        confidence=0.7,
    )


def _completed_review(
    run_id,
    chunk_number,
    *,
    hash_,
    category=ReviewFindingCategory.REQUIREMENT_MISMATCH,
    severity=ReviewFindingSeverity.HIGH,
):
    return ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=ChunkReviewVerdict.RISKY,
        summary="Human should review.",
        findings=[_finding(category=category, severity=severity)],
        reviewed_test_checkpoint_hash=hash_,
        provider="fakeprov",
        model="fakemodel",
    )


def _empty_review(run_id, chunk_number, *, hash_, status):
    return ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        review_status=status,
        reviewed_test_checkpoint_hash=hash_,
        provider="fakeprov",
        model="fakemodel",
    )


def _approve_chunk(run_id, chunk_number=1):
    return client.post(f"/runs/{run_id}/chunks/{chunk_number}/approve")


def _ack_review(run_id, chunk_number=1):
    return client.post(
        f"/runs/{run_id}/chunks/{chunk_number}/review/acknowledge",
        json={"reason": "human reviewed the advisory finding"},
    )


def _ack_test_validation(run_id, chunk_number=1):
    return client.post(
        f"/runs/{run_id}/chunks/{chunk_number}/test-validation/acknowledge",
        json={"reason": "human accepted weak validation"},
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


def _patch_chunk_approve(monkeypatch):
    calls: list[tuple[str, int]] = []

    def fake_approve(run_id, chunk_number):
        calls.append((run_id, chunk_number))
        return {
            "status": "chunk_approved",
            "run_id": run_id,
            "chunk_number": chunk_number,
        }

    monkeypatch.setattr(chunks_route, "approve_chunk_and_commit", fake_approve)
    return calls


def test_review_ack_decision_is_pure():
    decision = evaluate_review_ack_eligibility([])
    assert decision.eligible is True
    assert decision.reason == REVIEW_FINDING_ACK_ELIGIBLE

    missing = ReviewAckRequirement(
        chunk_number=1,
        state=REVIEW_ACK_MISSING,
        current_diff_hash="HASH",
        finding_count=1,
        categories=("security",),
    )
    blocked = evaluate_review_ack_eligibility([missing])
    assert blocked.eligible is False
    assert blocked.reason == REVIEW_FINDING_ACK_REQUIRED
    assert blocked.status_code == 409
    assert blocked.blocked_requirements == (missing,)


def test_no_review_fails_open_at_chunk_and_final_gates(tmp_path, tracked_runs, monkeypatch):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    calls = _patch_chunk_approve(monkeypatch)

    assert _approve_chunk(run_id).status_code == 200
    assert calls == [(run_id, 1)]

    update_chunk_status(run_id, 1, "completed")
    create_final_approval_gate(run_id, "final summary")
    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


@pytest.mark.parametrize(
    "status",
    [ChunkReviewStatus.UNAVAILABLE, ChunkReviewStatus.FAILED],
)
def test_unavailable_or_failed_review_never_gates_chunk_approval(
    tmp_path, tracked_runs, monkeypatch, status
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_empty_review(run_id, 1, hash_="HASH-A", status=status))
    calls = _patch_chunk_approve(monkeypatch)

    assert _approve_chunk(run_id).status_code == 200
    assert calls == [(run_id, 1)]


def test_stale_high_a1_review_fails_open(tmp_path, tracked_runs, monkeypatch):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-NEW")
    create_review(_completed_review(run_id, 1, hash_="HASH-OLD"))
    calls = _patch_chunk_approve(monkeypatch)

    assert _approve_chunk(run_id).status_code == 200
    assert calls == [(run_id, 1)]


def test_display_overlay_failure_does_not_block_approval(
    tmp_path, tracked_runs, monkeypatch
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")

    def boom(run_id, chunk_number):
        raise RuntimeError("display read failed")

    monkeypatch.setattr(chunks_route, "load_chunk_review_read_model", boom)
    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    assert response.json()["chunks"][0]["review"] is None

    calls = _patch_chunk_approve(monkeypatch)
    assert _approve_chunk(run_id).status_code == 200
    assert calls == [(run_id, 1)]


def test_enforcement_selector_exception_fails_open_at_chunk_gate(
    tmp_path, tracked_runs, monkeypatch
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    calls = _patch_chunk_approve(monkeypatch)

    def boom(run_id, chunk_number):
        raise RuntimeError("selector down")

    monkeypatch.setattr(
        chunks_route.review_finding_ack_store,
        "safe_chunk_requiring_review_ack",
        boom,
    )

    assert _approve_chunk(run_id).status_code == 200
    assert calls == [(run_id, 1)]


def test_enforcement_selector_exception_fails_open_at_final_gate(
    tmp_path, tracked_runs, monkeypatch
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    update_chunk_status(run_id, 1, "completed")
    create_final_approval_gate(run_id, "final summary")

    def boom(run_id):
        raise RuntimeError("selector down")

    monkeypatch.setattr(
        chunks_route.review_finding_ack_store,
        "chunks_requiring_review_ack",
        boom,
    )

    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


@pytest.mark.parametrize(
    "category",
    [ReviewFindingCategory.REQUIREMENT_MISMATCH, ReviewFindingCategory.SECURITY],
)
def test_current_high_a1_review_blocks_chunk_until_acknowledged(
    tmp_path, tracked_runs, monkeypatch, category
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A", category=category))
    calls = _patch_chunk_approve(monkeypatch)

    blocked = _approve_chunk(run_id)
    assert blocked.status_code == 409
    assert calls == []

    ack = _ack_review(run_id)
    assert ack.status_code == 200
    assert ack.json()["acknowledged_diff_hash"] == "HASH-A"

    assert _approve_chunk(run_id).status_code == 200
    assert calls == [(run_id, 1)]


def test_current_high_a1_review_blocks_final_until_acknowledged(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    update_chunk_status(run_id, 1, "completed")
    create_review(_completed_review(
        run_id,
        1,
        hash_="HASH-A",
        category=ReviewFindingCategory.SECURITY,
    ))
    create_final_approval_gate(run_id, "final summary")

    assert _approve_final(run_id).status_code == 409
    assert _run_status(run_id) != "final_approved"

    assert _ack_review(run_id).status_code == 200
    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


@pytest.mark.parametrize(
    "category",
    [
        ReviewFindingCategory.MAINTAINABILITY,
        ReviewFindingCategory.CORRECTNESS,
        ReviewFindingCategory.TEST_GAP,
        ReviewFindingCategory.SCOPE,
        ReviewFindingCategory.UNCERTAINTY,
    ],
)
def test_high_non_a1_categories_do_not_gate_by_default(category):
    review = ChunkReviewRecord(
        id="r",
        run_id="run",
        chunk_number=1,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=ChunkReviewVerdict.RISKY,
        findings=[_finding(category=category)],
        reviewed_test_checkpoint_hash="HASH",
    )

    assert review_findings_requiring_ack(review, "HASH") == ()


@pytest.mark.parametrize(
    "severity",
    [ReviewFindingSeverity.WARNING, ReviewFindingSeverity.INFO],
)
def test_warning_and_info_a1_findings_do_not_gate(severity):
    review = ChunkReviewRecord(
        id="r",
        run_id="run",
        chunk_number=1,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=ChunkReviewVerdict.NEEDS_HUMAN_ATTENTION,
        findings=[_finding(severity=severity)],
        reviewed_test_checkpoint_hash="HASH",
    )

    assert review_findings_requiring_ack(review, "HASH") == ()


def test_policy_categories_are_single_sourced(monkeypatch):
    monkeypatch.setattr(
        policy,
        "REVIEW_ACK_REQUIRED_CATEGORIES",
        frozenset({"maintainability"}),
    )
    review = ChunkReviewRecord(
        id="r",
        run_id="run",
        chunk_number=1,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=ChunkReviewVerdict.RISKY,
        findings=[_finding(category=ReviewFindingCategory.MAINTAINABILITY)],
        reviewed_test_checkpoint_hash="HASH",
    )

    assert len(review_findings_requiring_ack(review, "HASH")) == 1


def test_review_ack_and_test_validation_ack_are_independent(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_weak_verdict(run_id, 1)
    _seed_diff_hash(run_id, 1, "HASH-A")
    update_chunk_status(run_id, 1, "completed")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))
    create_final_approval_gate(run_id, "final summary")

    assert _ack_test_validation(run_id).status_code == 200
    # Test-validation acknowledgement does not satisfy reviewer acknowledgement.
    assert _approve_final(run_id).status_code == 409

    assert _ack_review(run_id).status_code == 200
    assert _approve_final(run_id).status_code == 200
    assert _run_status(run_id) == "final_approved"


def test_review_ack_stales_when_diff_identity_moves(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-1")
    update_chunk_status(run_id, 1, "completed")
    create_review(_completed_review(run_id, 1, hash_="HASH-1"))
    assert _ack_review(run_id).status_code == 200
    assert _ack_review(run_id).status_code == 200

    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*)
            FROM review_finding_acknowledgements
            WHERE run_id = :run_id AND chunk_number = 1 AND status = 'active'
        """), {"run_id": run_id}).scalar()
    assert count == 1

    _seed_diff_hash(run_id, 1, "HASH-2")
    create_review(_completed_review(run_id, 1, hash_="HASH-2"))
    create_final_approval_gate(run_id, "final summary")

    assert _approve_final(run_id).status_code == 409
    assert _ack_review(run_id).status_code == 200
    assert _approve_final(run_id).status_code == 200


def test_review_ack_row_stores_metadata_only(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))

    assert _ack_review(run_id).status_code == 200

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT run_id, chunk_number, acknowledged_diff_hash, reason,
                   acknowledged_at, status
            FROM review_finding_acknowledgements
            WHERE run_id = :run_id AND chunk_number = 1
        """), {"run_id": run_id}).fetchone()
        columns = {
            item._mapping["name"]
            for item in conn.execute(
                text("PRAGMA table_info(review_finding_acknowledgements)")
            ).fetchall()
        }
    data = dict(row._mapping)
    assert data["run_id"] == run_id
    assert data["acknowledged_diff_hash"] == "HASH-A"
    assert data["reason"] == "human reviewed the advisory finding"
    assert data["acknowledged_at"] is not None
    assert data["status"] == "active"
    assert "findings_json" not in columns
    assert "diff" not in columns
    assert "provider_error" not in columns


def test_read_model_surfaces_review_ack_state(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))

    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    review = response.json()["chunks"][0]["review"]
    assert review["requires_acknowledgement"] is True
    assert review["acknowledgement_status"] == "missing"
    assert review["acknowledgement_required_finding_count"] == 1
    assert review["acknowledgement_required_categories"] == ["requirement_mismatch"]

    assert _ack_review(run_id).status_code == 200
    response = client.get(f"/runs/{run_id}/chunks")
    assert response.json()["chunks"][0]["review"]["acknowledgement_status"] == "current"


def test_awaiting_chunk_approval_steer_stays_422_and_mutates_nothing(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")

    response = client.post(
        f"/runs/{run_id}/chunks/1/steer",
        json={"steer_text": "Address the reviewer finding."},
    )

    assert response.status_code == 422
    assert response.json()["status"] == "retry_ineligible"
    with engine.connect() as conn:
        turns = conn.execute(
            text("SELECT COUNT(*) FROM run_turns WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar()
        status = conn.execute(
            text("""
                SELECT status FROM chunks
                WHERE run_id = :run_id AND chunk_number = 1
            """),
            {"run_id": run_id},
        ).scalar()
    assert turns == 0
    assert status == "awaiting_chunk_approval"


def test_finding_to_steer_text_is_deterministic_and_scope_conservative():
    finding = _finding(category=ReviewFindingCategory.SECURITY)

    first = finding_to_steer_text(finding)
    second = finding_to_steer_text(finding)

    assert first == second
    assert "Address reviewer finding (high security)" in first
    assert "Stay within the already approved chunk scope" in first
