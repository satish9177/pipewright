"""
test_reviewer_models.py
Pure model + staleness tests for the Adversarial Reviewer Stage v1 foundation.

No DB, no git, no LLM, no routes. These cover that a completed review may carry a
verdict/findings, that a failed/unavailable review is provably empty, finding
validation, and the pure current/stale/missing classifier.
"""

import pytest
from pydantic import ValidationError

from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ChunkReviewStatus,
    ChunkReviewVerdict,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewStalenessStatus,
    classify_review_staleness,
)

pytestmark = pytest.mark.unit


def _finding(**overrides) -> ReviewFinding:
    base = {
        "category": ReviewFindingCategory.CORRECTNESS,
        "severity": ReviewFindingSeverity.WARNING,
        "title": "Possible off-by-one",
        "explanation": "The loop bound may exclude the final element.",
    }
    base.update(overrides)
    return ReviewFinding(**base)


def _record(**overrides) -> ChunkReviewRecord:
    base = {
        "id": "review-1",
        "run_id": "run-1",
        "chunk_number": 1,
        "review_status": ChunkReviewStatus.COMPLETED,
        "verdict": ChunkReviewVerdict.APPROVE_WITH_NOTES,
    }
    base.update(overrides)
    return ChunkReviewRecord(**base)


# --- completed reviews -------------------------------------------------------

def test_completed_review_can_carry_verdict_and_findings():
    record = _record(
        verdict=ChunkReviewVerdict.NEEDS_HUMAN_ATTENTION,
        summary="One concern worth a human check.",
        findings=[_finding()],
        test_gap_summary="No test exercises the new branch.",
        reviewed_test_checkpoint_hash="abc123",
    )
    assert record.review_status == ChunkReviewStatus.COMPLETED
    assert record.verdict == ChunkReviewVerdict.NEEDS_HUMAN_ATTENTION
    assert len(record.findings) == 1
    assert record.findings[0].affected_files == []


def test_completed_review_requires_a_verdict():
    with pytest.raises(ValidationError):
        _record(review_status=ChunkReviewStatus.COMPLETED, verdict=None)


def test_completed_review_with_empty_findings_is_valid():
    record = _record(findings=[])
    assert record.findings == []


# --- non-completed reviews must be provably empty ----------------------------

@pytest.mark.parametrize(
    "status",
    [ChunkReviewStatus.FAILED, ChunkReviewStatus.UNAVAILABLE],
)
def test_non_completed_review_cannot_carry_verdict(status):
    with pytest.raises(ValidationError):
        _record(review_status=status, verdict=ChunkReviewVerdict.RISKY)


@pytest.mark.parametrize(
    "status",
    [ChunkReviewStatus.FAILED, ChunkReviewStatus.UNAVAILABLE],
)
def test_non_completed_review_cannot_carry_findings(status):
    with pytest.raises(ValidationError):
        _record(review_status=status, verdict=None, findings=[_finding()])


@pytest.mark.parametrize(
    "status",
    [ChunkReviewStatus.FAILED, ChunkReviewStatus.UNAVAILABLE],
)
def test_non_completed_review_cannot_carry_summary(status):
    with pytest.raises(ValidationError):
        _record(
            review_status=status,
            verdict=None,
            summary="leaked content",
        )


def test_unavailable_review_with_no_content_is_valid():
    record = _record(
        review_status=ChunkReviewStatus.UNAVAILABLE,
        verdict=None,
        reviewed_test_checkpoint_hash="abc123",
        provider="example",
        model="example-model",
    )
    assert record.review_status == ChunkReviewStatus.UNAVAILABLE
    assert record.verdict is None
    assert record.findings == []


# --- finding validation ------------------------------------------------------

def test_finding_requires_non_blank_title():
    with pytest.raises(ValidationError):
        _finding(title="   ")


def test_finding_requires_non_blank_explanation():
    with pytest.raises(ValidationError):
        _finding(explanation="")


def test_finding_affected_files_defaults_empty():
    finding = _finding()
    assert finding.affected_files == []
    assert finding.suggested_human_check == ""


def test_finding_confidence_is_bounded_when_provided():
    assert _finding(confidence=0.5).confidence == 0.5
    with pytest.raises(ValidationError):
        _finding(confidence=1.5)


# --- pure staleness classifier ----------------------------------------------

def test_staleness_missing_when_no_review():
    assert classify_review_staleness(None, "abc123") == ReviewStalenessStatus.MISSING


def test_staleness_current_when_hashes_match():
    record = _record(reviewed_test_checkpoint_hash="abc123")
    assert classify_review_staleness(record, "abc123") == ReviewStalenessStatus.CURRENT


def test_staleness_stale_when_hashes_differ():
    record = _record(reviewed_test_checkpoint_hash="abc123")
    assert classify_review_staleness(record, "def456") == ReviewStalenessStatus.STALE


def test_staleness_stale_when_current_hash_indeterminate():
    record = _record(reviewed_test_checkpoint_hash="abc123")
    assert classify_review_staleness(record, None) == ReviewStalenessStatus.STALE
    assert classify_review_staleness(record, "  ") == ReviewStalenessStatus.STALE


def test_staleness_stale_when_review_hash_blank():
    record = _record(reviewed_test_checkpoint_hash=None)
    assert classify_review_staleness(record, "abc123") == ReviewStalenessStatus.STALE
