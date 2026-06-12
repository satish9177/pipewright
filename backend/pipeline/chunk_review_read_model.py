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

from backend.models.chunk import (
    ChunkReviewFindingReadModel,
    ChunkReviewReadModel,
    ReviewerIndependenceReadModel,
)
from backend.pipeline.chunk_review_store import (
    current_chunk_review_identity,
    get_latest_review,
)
from backend.pipeline.llm_call_provenance_store import get_provenance_for_chunk
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ReviewStalenessStatus,
    classify_review_staleness,
)
from backend.pipeline.review_finding_ack_store import (
    REVIEW_ACK_NOT_REQUIRED,
    finding_to_steer_text,
    review_ack_display_state,
)

logger = logging.getLogger(__name__)

# Display-only copy for each independence state (#33C).
_INDEPENDENCE_MESSAGES: dict[str, str] = {
    "self_review": (
        "Reviewer used the same provider/model as the coder; treat this review as "
        "a self-check, not an independent review."
    ),
    "independent": "Reviewer used a different provider/model from the coder.",
    "unknown": (
        "Coder provider provenance unavailable; reviewer independence could not be "
        "verified."
    ),
    "unavailable": (
        "No completed advisory review is available, so reviewer independence does "
        "not apply."
    ),
}


def compute_reviewer_independence(
    *,
    review_status: str,
    reviewer_provider: str | None,
    reviewer_model: str | None,
    coder_provider: str | None,
    coder_model: str | None,
) -> ReviewerIndependenceReadModel:
    """
    Pure independence classification from PERSISTED provenance. No I/O.

    Decided only from the values passed in (the reviewer's stored provider/model and
    the coder's persisted provenance for the same run/chunk) — never from current
    env config. Missing coder provenance yields ``unknown``, never a false
    ``independent``.
    """
    if review_status != "completed":
        status = "unavailable"
    elif not reviewer_provider or not reviewer_model:
        status = "unknown"
    elif not coder_provider or not coder_model:
        status = "unknown"
    elif reviewer_provider == coder_provider and reviewer_model == coder_model:
        status = "self_review"
    else:
        status = "independent"

    return ReviewerIndependenceReadModel(
        status=status,
        coder_provider=coder_provider,
        coder_model=coder_model,
        reviewer_provider=reviewer_provider,
        reviewer_model=reviewer_model,
        message=_INDEPENDENCE_MESSAGES[status],
    )


def to_read_model(
    record: ChunkReviewRecord,
    staleness: ReviewStalenessStatus,
    *,
    coder_provider: str | None = None,
    coder_model: str | None = None,
    requires_acknowledgement: bool = False,
    acknowledgement_status: str = REVIEW_ACK_NOT_REQUIRED,
    acknowledgement_required_finding_count: int = 0,
    acknowledgement_required_categories: tuple[str, ...] = (),
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
            steer_text=finding_to_steer_text(finding),
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
        reviewer_independence=compute_reviewer_independence(
            review_status=record.review_status.value,
            reviewer_provider=record.provider,
            reviewer_model=record.model,
            coder_provider=coder_provider,
            coder_model=coder_model,
        ),
        requires_acknowledgement=requires_acknowledgement,
        acknowledgement_status=acknowledgement_status,
        acknowledgement_required_finding_count=acknowledgement_required_finding_count,
        acknowledgement_required_categories=list(acknowledgement_required_categories),
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
        try:
            (
                requires_ack,
                ack_state,
                ack_finding_count,
                ack_categories,
            ) = review_ack_display_state(
                run_id,
                chunk_number,
                record,
                current_hash,
            )
        except Exception:
            requires_ack = False
            ack_state = REVIEW_ACK_NOT_REQUIRED
            ack_finding_count = 0
            ack_categories = ()
        coder_provider, coder_model = _latest_coder_provenance(run_id, chunk_number)
        return to_read_model(
            record,
            staleness,
            coder_provider=coder_provider,
            coder_model=coder_model,
            requires_acknowledgement=requires_ack,
            acknowledgement_status=ack_state,
            acknowledgement_required_finding_count=ack_finding_count,
            acknowledgement_required_categories=ack_categories,
        )
    except Exception as error:
        logger.warning(
            "[REVIEWER] review overlay skipped (read-only, fail-closed) | "
            "run_id=%s | chunk=%s | error=%s",
            run_id, chunk_number, error,
        )
        return None


def _latest_coder_provenance(
    run_id: str,
    chunk_number: int,
) -> tuple[str | None, str | None]:
    """
    The most-recent persisted coder provider/model for this run/chunk, or
    ``(None, None)``. Best-effort and read-only: any failure (or no coder
    provenance) yields ``(None, None)``, which the classifier maps to ``unknown``
    rather than a false ``independent``. ``get_provenance_for_chunk`` returns rows
    newest-first, so ``rows[0]`` is the effective coder call deterministically.
    """
    try:
        rows = get_provenance_for_chunk(run_id, chunk_number, role="coder")
        if not rows:
            return None, None
        return rows[0].provider, rows[0].model
    except Exception:
        return None, None
