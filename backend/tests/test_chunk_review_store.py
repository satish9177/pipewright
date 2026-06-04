"""
test_chunk_review_store.py
Storage tests for the isolated advisory-review foundation.

Pure backend DB CRUD: no git, no LLM, no routes, no real test execution. Reviews
are seeded directly. Run ids are synthetic (SQLite FK enforcement is off in this
engine), keeping the store tests isolated from runs/chunks/checkpoints. Cleanup
only touches chunk_reviews rows.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline.chunk_review_store import (
    create_review,
    current_chunk_review_identity,
    get_latest_review,
    get_review_by_checkpoint_hash,
    review_staleness,
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


def _new_run(tracked_runs) -> str:
    run_id = f"rev-test-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    return run_id


def _completed(run_id: str, chunk_number: int, *, hash_: str, **overrides) -> ChunkReviewRecord:
    base = {
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "chunk_number": chunk_number,
        "review_status": ChunkReviewStatus.COMPLETED,
        "verdict": ChunkReviewVerdict.APPROVE_WITH_NOTES,
        "summary": "Looks reasonable; one note.",
        "findings": [
            ReviewFinding(
                category=ReviewFindingCategory.TEST_GAP,
                severity=ReviewFindingSeverity.WARNING,
                title="New branch untested",
                explanation="No test exercises the added branch.",
                affected_files=["src/app.py"],
            )
        ],
        "reviewed_test_checkpoint_hash": hash_,
    }
    base.update(overrides)
    return ChunkReviewRecord(**base)


def test_create_and_get_latest_review(tracked_runs):
    run_id = _new_run(tracked_runs)
    stored = create_review(_completed(run_id, 1, hash_="hash-A"))

    fetched = get_latest_review(run_id, 1)
    assert fetched is not None
    assert fetched.id == stored.id
    assert fetched.review_status == ChunkReviewStatus.COMPLETED
    assert fetched.verdict == ChunkReviewVerdict.APPROVE_WITH_NOTES
    assert fetched.reviewed_test_checkpoint_hash == "hash-A"
    assert len(fetched.findings) == 1
    assert fetched.findings[0].category == ReviewFindingCategory.TEST_GAP
    assert fetched.findings[0].affected_files == ["src/app.py"]


def test_missing_review_is_none_and_missing_staleness(tracked_runs):
    run_id = _new_run(tracked_runs)
    assert get_latest_review(run_id, 1) is None
    assert review_staleness(run_id, 1, "hash-A") == ReviewStalenessStatus.MISSING


def test_matching_hash_is_current(tracked_runs):
    run_id = _new_run(tracked_runs)
    create_review(_completed(run_id, 1, hash_="hash-A"))
    assert review_staleness(run_id, 1, "hash-A") == ReviewStalenessStatus.CURRENT


def test_mismatched_hash_is_stale(tracked_runs):
    run_id = _new_run(tracked_runs)
    create_review(_completed(run_id, 1, hash_="hash-A"))
    assert review_staleness(run_id, 1, "hash-B") == ReviewStalenessStatus.STALE


def test_indeterminate_current_hash_is_stale(tracked_runs):
    run_id = _new_run(tracked_runs)
    create_review(_completed(run_id, 1, hash_="hash-A"))
    assert review_staleness(run_id, 1, None) == ReviewStalenessStatus.STALE


def test_get_latest_returns_newest_of_multiple(tracked_runs):
    run_id = _new_run(tracked_runs)
    # Distinct created_at so ordering is deterministic (older then newer).
    create_review(_completed(
        run_id, 1, hash_="hash-A", created_at="2024-01-01T00:00:00+00:00",
    ))
    newer = create_review(_completed(
        run_id, 1, hash_="hash-B", created_at="2024-02-01T00:00:00+00:00",
    ))

    latest = get_latest_review(run_id, 1)
    assert latest is not None
    assert latest.id == newer.id
    assert latest.reviewed_test_checkpoint_hash == "hash-B"
    # Latest review identity drives staleness: the new hash is current, old is stale.
    assert review_staleness(run_id, 1, "hash-B") == ReviewStalenessStatus.CURRENT
    assert review_staleness(run_id, 1, "hash-A") == ReviewStalenessStatus.STALE


def test_get_review_by_checkpoint_hash(tracked_runs):
    run_id = _new_run(tracked_runs)
    create_review(_completed(run_id, 1, hash_="hash-A"))
    create_review(_completed(run_id, 1, hash_="hash-B"))

    by_a = get_review_by_checkpoint_hash(run_id, 1, "hash-A")
    assert by_a is not None
    assert by_a.reviewed_test_checkpoint_hash == "hash-A"

    assert get_review_by_checkpoint_hash(run_id, 1, "nope") is None
    # A blank/None hash never matches an indeterminate identity.
    assert get_review_by_checkpoint_hash(run_id, 1, None) is None
    assert get_review_by_checkpoint_hash(run_id, 1, "   ") is None


def test_unavailable_review_roundtrip_is_empty(tracked_runs):
    run_id = _new_run(tracked_runs)
    record = ChunkReviewRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=2,
        review_status=ChunkReviewStatus.UNAVAILABLE,
        reviewed_test_checkpoint_hash="hash-A",
        provider="example",
        model="example-model",
    )
    create_review(record)

    fetched = get_latest_review(run_id, 2)
    assert fetched is not None
    assert fetched.review_status == ChunkReviewStatus.UNAVAILABLE
    assert fetched.verdict is None
    assert fetched.findings == []
    assert fetched.summary is None


def test_store_does_not_touch_checkpoints_or_chunks(tracked_runs):
    """Creating a review writes ONLY chunk_reviews — not checkpoints or chunks."""
    run_id = _new_run(tracked_runs)
    create_review(_completed(run_id, 1, hash_="hash-A"))

    with engine.connect() as conn:
        reviews = conn.execute(
            text("SELECT COUNT(*) FROM chunk_reviews WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()
        checkpoints = conn.execute(
            text("SELECT COUNT(*) FROM checkpoints WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()
        chunks = conn.execute(
            text("SELECT COUNT(*) FROM chunks WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()
    assert reviews == 1
    assert checkpoints == 0
    assert chunks == 0


def test_current_identity_reuses_checkpoint_hash_and_is_safe_when_missing(tracked_runs):
    """
    current_chunk_review_identity reuses the #28F helper and returns None when no
    test checkpoint exists. With no review, staleness is MISSING; with a review but
    an indeterminate current identity, staleness is STALE (never falsely current).
    """
    run_id = _new_run(tracked_runs)
    assert current_chunk_review_identity(run_id, 1) is None

    assert review_staleness(
        run_id, 1, current_chunk_review_identity(run_id, 1)
    ) == ReviewStalenessStatus.MISSING

    create_review(_completed(run_id, 1, hash_="hash-A"))
    assert review_staleness(
        run_id, 1, current_chunk_review_identity(run_id, 1)
    ) == ReviewStalenessStatus.STALE
