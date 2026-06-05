"""
test_reviewer_independence.py
Tests for #33C reviewer-independence disclosure.

Independence is derived ON READ from PERSISTED provenance only: the reviewer's
stored provider/model (chunk_reviews) vs the coder's persisted llm_call_provenance
row for the same run/chunk. It never reads env config and never gates anything.

Pure backend DB CRUD + a pure classifier: no git, no LLM, no routes. Cleanup only
touches chunk_reviews and llm_call_provenance rows for the synthetic run ids.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline.chunk_review_read_model import (
    compute_reviewer_independence,
    load_chunk_review_read_model,
)
from backend.pipeline.chunk_review_store import create_review
from backend.pipeline.llm_call_provenance_store import (
    LLMCallProvenanceRecord,
    record_llm_call_provenance,
)
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ChunkReviewStatus,
    ChunkReviewVerdict,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    init_db()
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(
                text("DELETE FROM chunk_reviews WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM llm_call_provenance WHERE run_id = :run_id"),
                {"run_id": run_id},
            )


def _new_run(tracked_runs) -> str:
    run_id = f"indep-test-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    return run_id


def _seed_completed_review(run_id, chunk_number, *, provider, model):
    create_review(ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        review_status=ChunkReviewStatus.COMPLETED,
        verdict=ChunkReviewVerdict.APPROVE_WITH_NOTES,
        summary="ok",
        provider=provider,
        model=model,
    ))


def _seed_unavailable_review(run_id, chunk_number, *, provider, model):
    create_review(ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        review_status=ChunkReviewStatus.UNAVAILABLE,
        provider=provider,
        model=model,
    ))


def _seed_coder_provenance(run_id, chunk_number, *, provider, model, created_at=None):
    record_llm_call_provenance(LLMCallProvenanceRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=chunk_number,
        role="coder",
        provider=provider,
        model=model,
        created_at=created_at,
    ))


# --- pure classifier ---------------------------------------------------------

def test_compute_self_review_when_provider_and_model_match():
    result = compute_reviewer_independence(
        review_status="completed",
        reviewer_provider="gemini", reviewer_model="gemini-2.5-flash-lite",
        coder_provider="gemini", coder_model="gemini-2.5-flash-lite",
    )
    assert result.status == "self_review"
    assert "self-check" in result.message


def test_compute_independent_when_provider_or_model_differ():
    different_provider = compute_reviewer_independence(
        review_status="completed",
        reviewer_provider="gemini", reviewer_model="m",
        coder_provider="anthropic", coder_model="m",
    )
    different_model = compute_reviewer_independence(
        review_status="completed",
        reviewer_provider="gemini", reviewer_model="m1",
        coder_provider="gemini", coder_model="m2",
    )
    assert different_provider.status == "independent"
    assert different_model.status == "independent"


def test_compute_unknown_when_coder_provenance_missing():
    result = compute_reviewer_independence(
        review_status="completed",
        reviewer_provider="gemini", reviewer_model="m",
        coder_provider=None, coder_model=None,
    )
    assert result.status == "unknown"  # never a false "independent"


def test_compute_unknown_when_reviewer_provenance_missing():
    result = compute_reviewer_independence(
        review_status="completed",
        reviewer_provider=None, reviewer_model=None,
        coder_provider="gemini", coder_model="m",
    )
    assert result.status == "unknown"


def test_compute_unavailable_when_review_not_completed():
    for status in ("unavailable", "failed"):
        result = compute_reviewer_independence(
            review_status=status,
            reviewer_provider="gemini", reviewer_model="m",
            coder_provider="anthropic", coder_model="m2",
        )
        assert result.status == "unavailable"


# --- read-model integration --------------------------------------------------

def test_load_self_review(tracked_runs):
    run_id = _new_run(tracked_runs)
    _seed_coder_provenance(run_id, 1, provider="gemini", model="gemini-2.5-flash-lite")
    _seed_completed_review(run_id, 1, provider="gemini", model="gemini-2.5-flash-lite")

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    ind = overlay.reviewer_independence
    assert ind is not None
    assert ind.status == "self_review"
    assert ind.coder_provider == "gemini"
    assert ind.reviewer_provider == "gemini"


def test_load_independent(tracked_runs):
    run_id = _new_run(tracked_runs)
    _seed_coder_provenance(run_id, 1, provider="anthropic", model="claude-x")
    _seed_completed_review(run_id, 1, provider="gemini", model="gemini-2.5-flash-lite")

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.reviewer_independence.status == "independent"
    assert overlay.reviewer_independence.coder_provider == "anthropic"


def test_load_unknown_when_no_coder_provenance(tracked_runs):
    run_id = _new_run(tracked_runs)
    _seed_completed_review(run_id, 1, provider="gemini", model="gemini-2.5-flash-lite")

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.reviewer_independence.status == "unknown"
    assert overlay.reviewer_independence.coder_provider is None


def test_load_unavailable_review_preserves_behavior(tracked_runs):
    run_id = _new_run(tracked_runs)
    # Coder provenance EXISTS, but the review itself is unavailable: independence
    # must be 'unavailable', and the existing unavailable overlay is unchanged.
    _seed_coder_provenance(run_id, 1, provider="gemini", model="m")
    _seed_unavailable_review(run_id, 1, provider="gemini", model="m")

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay is not None
    assert overlay.review_status == "unavailable"
    assert overlay.verdict is None
    assert overlay.findings == []
    assert overlay.reviewer_independence.status == "unavailable"


def test_load_missing_review_returns_none_unchanged(tracked_runs):
    run_id = _new_run(tracked_runs)
    # No review row at all: still None (missing), regardless of coder provenance.
    _seed_coder_provenance(run_id, 1, provider="gemini", model="m")
    assert load_chunk_review_read_model(run_id, 1) is None


def test_most_recent_coder_provenance_wins(tracked_runs):
    run_id = _new_run(tracked_runs)
    # Older row differs; newest row matches the reviewer => self_review.
    _seed_coder_provenance(
        run_id, 1, provider="anthropic", model="claude-x",
        created_at="2020-01-01T00:00:00+00:00",
    )
    _seed_coder_provenance(
        run_id, 1, provider="gemini", model="gemini-2.5-flash-lite",
        created_at="2030-01-01T00:00:00+00:00",
    )
    _seed_completed_review(run_id, 1, provider="gemini", model="gemini-2.5-flash-lite")

    overlay = load_chunk_review_read_model(run_id, 1)
    assert overlay.reviewer_independence.status == "self_review"
    assert overlay.reviewer_independence.coder_provider == "gemini"


def test_independence_is_additive_only(tracked_runs):
    """The new field must not alter existing review overlay fields."""
    run_id = _new_run(tracked_runs)
    _seed_coder_provenance(run_id, 1, provider="gemini", model="m")
    _seed_completed_review(run_id, 1, provider="gemini", model="m")

    overlay = load_chunk_review_read_model(run_id, 1)
    # Pre-#33C fields unchanged for a completed review.
    assert overlay.review_status == "completed"
    assert overlay.verdict == "approve_with_notes"
    assert overlay.provider == "gemini"
    assert overlay.model == "m"
