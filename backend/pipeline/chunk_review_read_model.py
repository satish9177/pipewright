"""
chunk_review_read_model.py
Read-only adapter that turns a stored advisory review into the display overlay
attached to the chunk read response (Adversarial Reviewer v1;
docs/design/adversarial-reviewer-stage.md).

This is the READ side only. It performs no LLM call, runs no reviewer, mutates
nothing, and grants no authority — it just maps a persisted ChunkReviewRecord plus
an on-read staleness classification into ChunkReviewReadModel. Staleness reuses the
existing chunk diff/test-checkpoint identity (current_chunk_review_identity →
compute_chunk_diff_hash); no new hash scheme is introduced.

``load_chunk_review_read_model`` is fail-closed: any error yields ``None`` (treated
as "no review / missing") so the GET chunk route can never 500 because advisory
review data failed to load or map.
"""

import logging

from backend.models.chunk import ChunkReviewFindingReadModel, ChunkReviewReadModel
from backend.pipeline.chunk_review_store import (
    current_chunk_review_identity,
    get_latest_review,
)
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ReviewStalenessStatus,
    classify_review_staleness,
)

logger = logging.getLogger(__name__)


def to_read_model(
    record: ChunkReviewRecord,
    staleness: ReviewStalenessStatus,
) -> ChunkReviewReadModel:
    """Pure mapping from a stored review + staleness to the display overlay."""
    findings = [
        ChunkReviewFindingReadModel(
            category=finding.category.value,
            severity=finding.severity.value,
            title=finding.title,
            explanation=finding.explanation,
            affected_files=list(finding.affected_files),
            suggested_human_check=finding.suggested_human_check,
            confidence=finding.confidence,
        )
        for finding in record.findings
    ]
    return ChunkReviewReadModel(
        review_status=record.review_status.value,
        staleness=staleness.value,
        verdict=record.verdict.value if record.verdict else None,
        summary=record.summary,
        findings=findings,
        test_gap_summary=record.test_gap_summary,
        scope_summary=record.scope_summary,
        security_or_safety_summary=record.security_or_safety_summary,
        recommended_human_action=record.recommended_human_action,
        reviewed_test_checkpoint_hash=record.reviewed_test_checkpoint_hash,
        checkpoint_id=record.checkpoint_id,
        provider=record.provider,
        model=record.model,
        created_at=record.created_at,
    )


def load_chunk_review_read_model(
    run_id: str,
    chunk_number: int,
) -> ChunkReviewReadModel | None:
    """
    Build the advisory review overlay for one chunk, or ``None`` when no review
    exists (missing). Fail-closed: returns ``None`` on any error and never raises,
    so a read-path problem degrades to "no review" rather than failing the route.
    No LLM call; staleness uses the existing diff/test-checkpoint identity.
    """
    try:
        record = get_latest_review(run_id, chunk_number)
        if record is None:
            return None
        try:
            current_hash = current_chunk_review_identity(run_id, chunk_number)
        except Exception:
            # An indeterminate identity must never read as current; classify_review_
            # staleness maps a None current_hash to STALE.
            current_hash = None
        staleness = classify_review_staleness(record, current_hash)
        return to_read_model(record, staleness)
    except Exception as error:
        logger.warning(
            "[REVIEWER] review overlay skipped (read-only, fail-closed) | "
            "run_id=%s | chunk=%s | error=%s",
            run_id, chunk_number, error,
        )
        return None
