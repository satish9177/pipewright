"""
reviewer_models.py
Pure models for the Adversarial Reviewer Stage v1 (design: docs/design/adversarial-reviewer-stage.md).

This module defines ONLY data shapes and a pure staleness classifier for a future
per-chunk, advisory AI review. It performs no I/O: no DB, no git, no LLM, no route
calls. It runs no reviewer and surfaces nothing — it is the model foundation that a
later execution/read slice builds on.

Hard properties of these models:

  - A review is **advisory evidence only**. Nothing here grants approval authority,
    commits, pushes, creates PRs, mutates files, writes memory, or gates any
    existing approval. It cannot weaken #26/#27/#28.
  - A review is bound to the chunk's **test-checkpoint / diff identity**
    (``reviewed_test_checkpoint_hash``), the same hash concept #28F already uses
    (``compute_chunk_diff_hash`` in
    backend/pipeline/test_validation_ack_store.py). We do NOT invent a separate
    diff-hash scheme, so review staleness and acknowledgement staleness cannot
    diverge.
  - ``current`` / ``stale`` / ``missing`` is computed on read against the chunk's
    current identity; it is never persisted.
  - Only a ``completed`` review may carry a verdict, findings, or summaries. A
    ``failed`` / ``unavailable`` review is representable and provably empty.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChunkReviewStatus(str, Enum):
    """Whether a review was produced at all. Only COMPLETED carries content."""

    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ChunkReviewVerdict(str, Enum):
    """
    Advisory verdict for a completed review. This is a recommendation to the human,
    never an authorization. ``APPROVE_WITH_NOTES`` does NOT mean "safe to merge".
    """

    APPROVE_WITH_NOTES = "approve_with_notes"
    NEEDS_HUMAN_ATTENTION = "needs_human_attention"
    RISKY = "risky"


class ReviewFindingCategory(str, Enum):
    CORRECTNESS = "correctness"
    TEST_GAP = "test_gap"
    SCOPE = "scope"
    SECURITY = "security"
    MAINTAINABILITY = "maintainability"
    REQUIREMENT_MISMATCH = "requirement_mismatch"
    UNCERTAINTY = "uncertainty"


class ReviewFindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"


class ReviewStalenessStatus(str, Enum):
    """
    Computed on read by comparing a review's bound identity to the chunk's current
    identity. Never persisted.

      - CURRENT: a review exists and matches the current diff/test-checkpoint hash.
      - STALE: a review exists but for a different (or indeterminate) identity.
      - MISSING: no review exists for the chunk.
    """

    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"


class ReviewFinding(BaseModel):
    """One advisory observation. Display hints only; grants no authority."""

    category: ReviewFindingCategory
    severity: ReviewFindingSeverity
    title: str
    explanation: str
    affected_files: list[str] = Field(default_factory=list)
    suggested_human_check: str = ""
    # Display-only. Not used to gate, rank-for-action, or authorize anything.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("title", "explanation")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("reviewer_models.py: finding field must not be blank")
        return value.strip()


class ChunkReviewRecord(BaseModel):
    """
    A single per-chunk advisory review record.

    Validation guarantees the failed/unavailable states are provably empty: a
    non-completed review carries no verdict, no findings, and no summary content,
    so a failed reviewer can never masquerade as a real verdict.
    """

    # ``model``/``provider`` are plain metadata strings; silence pydantic's
    # protected-namespace warning for the literal field name ``model``.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    run_id: str
    chunk_number: int = Field(..., ge=1)
    review_status: ChunkReviewStatus

    # Content fields — populated only when review_status == COMPLETED.
    verdict: ChunkReviewVerdict | None = None
    summary: str | None = None
    findings: list[ReviewFinding] = Field(default_factory=list)
    test_gap_summary: str | None = None
    scope_summary: str | None = None
    security_or_safety_summary: str | None = None
    recommended_human_action: str | None = None

    # Identity binding — the chunk's test-checkpoint / diff hash (#28F concept).
    reviewed_test_checkpoint_hash: str | None = None
    checkpoint_id: str | None = None

    # Sanitized provider/model metadata (never secrets/tokens).
    provider: str | None = None
    model: str | None = None

    created_at: str | None = None

    @model_validator(mode="after")
    def _validate_status_content(self) -> "ChunkReviewRecord":
        if self.review_status == ChunkReviewStatus.COMPLETED:
            if self.verdict is None:
                raise ValueError(
                    "reviewer_models.py: a completed review must carry a verdict"
                )
            return self

        # Not completed: must be provably empty of content.
        if self.verdict is not None:
            raise ValueError(
                "reviewer_models.py: a non-completed review must not carry a verdict"
            )
        if self.findings:
            raise ValueError(
                "reviewer_models.py: a non-completed review must not carry findings"
            )
        for field_name in (
            "summary",
            "test_gap_summary",
            "scope_summary",
            "security_or_safety_summary",
            "recommended_human_action",
        ):
            value = getattr(self, field_name)
            if value is not None and str(value).strip():
                raise ValueError(
                    "reviewer_models.py: a non-completed review must not carry "
                    f"summary content ({field_name})"
                )
        return self


def classify_review_staleness(
    review: ChunkReviewRecord | None,
    current_hash: str | None,
) -> ReviewStalenessStatus:
    """
    Pure staleness classification. No I/O.

    Mirrors #28F acknowledgement staleness exactly:
      - no review                          -> MISSING
      - review with a blank bound identity -> STALE (cannot be confirmed current)
      - indeterminate current identity     -> STALE (cannot be confirmed current)
      - bound identity == current identity -> CURRENT
      - otherwise                          -> STALE

    ``current_hash`` is supplied by the caller (e.g. from the existing
    ``compute_chunk_diff_hash``), so this stays a pure, side-effect-free decision
    and a stale review is never shown as current.
    """
    if review is None:
        return ReviewStalenessStatus.MISSING

    reviewed = review.reviewed_test_checkpoint_hash
    if not reviewed or not str(reviewed).strip():
        return ReviewStalenessStatus.STALE
    if not current_hash or not str(current_hash).strip():
        return ReviewStalenessStatus.STALE

    return (
        ReviewStalenessStatus.CURRENT
        if str(reviewed).strip() == str(current_hash).strip()
        else ReviewStalenessStatus.STALE
    )
