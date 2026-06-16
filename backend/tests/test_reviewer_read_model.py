"""
test_reviewer_read_model.py
Tests for the read-only advisory reviewer overlay on the chunk read response.

GET /runs/{run_id}/chunks augments each chunk with a display-only ``review`` overlay
built from chunk_reviews, with current/stale/missing computed on read against the
existing diff/test-checkpoint identity. Additive, fail-closed, no LLM call on read,
and it never affects test_validation, operator_state, or any approval. Reviews and
diff hashes are seeded directly — no git, no LLM, no real test execution.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import engine, init_db
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import chunk_review_read_model as read_model_module
from backend.pipeline import reviewer as reviewer_module
from backend.pipeline.chunk_review_read_model import (
    load_chunk_review_read_model,
    to_read_model,
)
from backend.pipeline.chunk_review_store import create_review
from backend.pipeline.chunk_store import (
    create_chunked_run,
    save_chunk_test_run_verdict,
    update_chunk_status,
)
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ChunkReviewStatus,
    ChunkReviewVerdict,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewStalenessStatus,
)
import backend.routes.chunks as chunks_route
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
            for table in ("chunk_reviews", "checkpoints", "chunks"):
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
        name=f"reviewrm-{uuid.uuid4()}",
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


def _seed_strong_verdict(run_id, chunk_number):
    result = classify_test_run("pytest", 0, "===== 5 passed in 0.10s =====")
    save_chunk_test_run_verdict(run_id, chunk_number, result)
    update_chunk_status(run_id, chunk_number, "completed")


def _completed_review(
    run_id,
    chunk_number,
    *,
    hash_,
    verdict=ChunkReviewVerdict.NEEDS_HUMAN_ATTENTION,
):
    return ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=verdict,
        summary="One concern worth a human check.",
        findings=[
            ReviewFinding(
                category=ReviewFindingCategory.TEST_GAP,
                severity=ReviewFindingSeverity.WARNING,
                title="New branch untested",
                explanation="No test exercises the added branch.",
                affected_files=["a.py"],
                suggested_human_check="Add a test.",
                confidence=0.4,
            )
        ],
        test_gap_summary="One untested branch.",
        reviewed_test_checkpoint_hash=hash_,
        provider="fakeprov",
        model="fakemodel",
    )


def _unavailable_review(run_id, chunk_number, *, hash_):
    return ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        review_status=ChunkReviewStatus.UNAVAILABLE,
        reviewed_test_checkpoint_hash=hash_,
        provider="fakeprov",
        model="fakemodel",
    )


# --- adapter / store-level ---------------------------------------------------

def test_missing_review_returns_none(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    assert load_chunk_review_read_model(run_id, 1) is None


def test_completed_current_review_overlay(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.review_status == "completed"
    assert overlay.staleness == "current"
    assert overlay.verdict == "needs_human_attention"
    assert overlay.summary == "One concern worth a human check."
    assert len(overlay.findings) == 1
    assert overlay.findings[0].category == "test_gap"
    assert overlay.findings[0].affected_files == ["a.py"]
    assert overlay.reviewed_test_checkpoint_hash == "HASH-A"
    assert overlay.provider == "fakeprov"


def test_stale_review_overlay(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-NEW")
    create_review(_completed_review(run_id, 1, hash_="HASH-OLD"))

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.staleness == "stale"
    assert overlay.verdict == "needs_human_attention"


def test_unavailable_review_overlay_has_no_content(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_unavailable_review(run_id, 1, hash_="HASH-A"))

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.review_status == "unavailable"
    assert overlay.verdict is None
    assert overlay.findings == []
    # Even unavailable, staleness is computed; matching hash => current.
    assert overlay.staleness == "current"


def test_indeterminate_identity_never_current(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    # No test checkpoint => current identity is None => never falsely current.
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))
    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.staleness == "stale"


def test_to_read_model_pure_mapping():
    record = ChunkReviewRecord(
        id="r",
        run_id="run",
        chunk_number=1,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=ChunkReviewVerdict.RISKY,
        summary="s",
        reviewed_test_checkpoint_hash="H",
    )
    overlay = to_read_model(record, ReviewStalenessStatus.CURRENT)
    assert overlay.review_status == "completed"
    assert overlay.verdict == "risky"
    assert overlay.staleness == "current"
    assert overlay.reviewed_test_checkpoint_hash == "H"


def test_load_is_fail_closed_on_store_error(monkeypatch, tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)

    def boom(run_id, chunk_number):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(read_model_module, "get_latest_review", boom)
    # Fail-closed: returns None rather than raising.
    assert load_chunk_review_read_model(run_id, 1) is None


# --- route-level -------------------------------------------------------------

def test_route_surfaces_review_and_preserves_overlays(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_strong_verdict(run_id, 1)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))

    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    body = response.json()

    # operator_state still present and unchanged in shape.
    assert body.get("operator_state") is not None
    chunk = body["chunks"][0]
    # #28 test_validation overlay still present.
    assert chunk.get("test_validation") is not None
    assert chunk["test_validation"]["verdict"] == "strong"
    # Advisory review overlay present and additive.
    review = chunk.get("review")
    assert review is not None
    assert review["review_status"] == "completed"
    assert review["staleness"] == "current"
    assert review["verdict"] == "needs_human_attention"
    assert review["findings"][0]["category"] == "test_gap"
    # No action ids / approval controls leak into the overlay.
    assert "action" not in review
    assert "actions" not in review


def test_route_operator_state_pr_ready_with_ok_review_stays_ready(
    tmp_path,
    tracked_runs,
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_strong_verdict(run_id, 1)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(
        _completed_review(
            run_id,
            1,
            hash_="HASH-A",
            verdict=ChunkReviewVerdict.APPROVE_WITH_NOTES,
        )
    )
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pipeline_runs
                SET status = 'complete',
                    pr_url = 'https://github.com/acme/demo/pull/7',
                    pr_number = 7
                WHERE id = :run_id
            """),
            {"run_id": run_id},
        )

    response = client.get(f"/runs/{run_id}/chunks")

    assert response.status_code == 200
    state = response.json()["operator_state"]
    assert state["title"] == "Pull request is ready"
    assert state["waiting_on"] == "nobody"
    assert "No further in-app action is required" in state["explanation"]


def test_route_operator_state_pr_ready_with_review_attention_needs_inspection(
    tmp_path,
    tracked_runs,
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_strong_verdict(run_id, 1)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pipeline_runs
                SET status = 'complete',
                    pr_url = 'https://github.com/acme/demo/pull/7',
                    pr_number = 7
                WHERE id = :run_id
            """),
            {"run_id": run_id},
        )

    response = client.get(f"/runs/{run_id}/chunks")

    assert response.status_code == 200
    body = response.json()
    assert body["chunks"][0]["review"]["verdict"] == "needs_human_attention"
    state = body["operator_state"]
    assert state["title"] == "Review findings need inspection"
    assert state["waiting_on"] == "human"
    assert state["primary_action"] is None
    assert "No further in-app action is required" not in state["explanation"]
    assert "advisory reviewer findings need human inspection" in state["explanation"]
    assert "before you merge" in state["explanation"]


def test_route_no_review_returns_null_and_200(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_strong_verdict(run_id, 1)

    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    chunk = response.json()["chunks"][0]
    assert chunk.get("review") is None
    # Existing overlays unaffected.
    assert chunk.get("test_validation") is not None


def test_route_surfaces_stale_review(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-NEW")
    create_review(_completed_review(run_id, 1, hash_="HASH-OLD"))

    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    review = response.json()["chunks"][0]["review"]
    assert review is not None
    assert review["staleness"] == "stale"


def test_route_failed_overlay_does_not_break_endpoint(monkeypatch, tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_strong_verdict(run_id, 1)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))

    def boom(run_id, chunk_number):
        raise RuntimeError("overlay mapping exploded")

    # Force the augmentation loop to raise; the route must still return 200 with
    # the plan unchanged (review omitted), never a 500.
    monkeypatch.setattr(chunks_route, "load_chunk_review_read_model", boom)

    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    chunk = response.json()["chunks"][0]
    assert chunk.get("review") is None
    # Other overlays still present despite the review failure.
    assert chunk.get("test_validation") is not None
    assert response.json().get("operator_state") is not None


def test_route_read_does_not_invoke_reviewer_llm(monkeypatch, tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_diff_hash(run_id, 1, "HASH-A")
    create_review(_completed_review(run_id, 1, hash_="HASH-A"))

    async def explode(*args, **kwargs):
        raise AssertionError("no LLM call may happen during a read")

    # If the read path ever routed through reviewer execution, this would fire.
    monkeypatch.setattr(reviewer_module, "complete_for_role", explode)

    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    assert response.json()["chunks"][0]["review"]["review_status"] == "completed"
