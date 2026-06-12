"""
review_finding_ack_store.py
Persistence + gate evaluation for reviewer-finding acknowledgements.

This is the Phase 4 item 15 sibling of test_validation_ack_store.py. It records
metadata-only human acknowledgements of a current advisory reviewer findings
panel. The acknowledgement is bound to the chunk's current #28F diff identity;
a retry, steer, or refinement that moves that identity makes the old ack stale.

Safety properties:

  - Reviewer findings are advisory. This module never approves, rejects,
    commits, pushes, creates PRs, runs reviewers, expands scope, or mutates code.
  - Only a delivered current high x {requirement_mismatch, security} finding
    requires acknowledgement by default. The policy set is single-sourced in
    backend.pipeline.policy.
  - Enforcement selectors fail open: missing, failed, unavailable, stale, or
    indeterminate review state yields no requirement. Only a successfully loaded
    current A1 finding can produce a blocking requirement, and a human ack always
    clears it.
  - Ack rows store metadata only: ids, chunk, diff hash, who/when, optional
    reason, status. They never store finding bodies, diffs, prompts, provider
    errors, secrets, or file contents.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline import policy
from backend.pipeline.chunk_review_store import (
    current_chunk_review_identity,
    get_review_by_checkpoint_hash,
)
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ChunkReviewStatus,
    ReviewFinding,
    ReviewStalenessStatus,
    classify_review_staleness,
)

REVIEW_ACK_STATUS_ACTIVE = "active"

REVIEW_ACK_NOT_REQUIRED = "not_required"
REVIEW_ACK_CURRENT = "current"
REVIEW_ACK_STALE = "stale"
REVIEW_ACK_MISSING = "missing"

REVIEW_FINDING_ACK_ELIGIBLE = "review_finding_acknowledgement_satisfied"
REVIEW_FINDING_ACK_REQUIRED = "review_finding_acknowledgement_required"


class ReviewFindingAckConflictError(Exception):
    """
    Raised when an acknowledgement cannot be recorded in the requested state.

    Distinct from ValueError (not found -> 404) so routes can map it to HTTP
    409. Nothing is written when this is raised.
    """


@dataclass(frozen=True)
class ReviewAckRequirement:
    """One chunk whose current reviewer findings panel must be acknowledged."""

    chunk_number: int
    state: str
    current_diff_hash: str | None
    finding_count: int
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewAckEligibilityDecision:
    """
    Pure route/read-model-friendly acknowledgement decision.

    Empty blocking requirements means approval is unblocked by reviewer ack.
    Non-empty means the route should return 409 before the approval mutation.
    """

    eligible: bool
    reason: str | None
    status_code: int | None
    blocked_requirements: tuple[ReviewAckRequirement, ...] = ()


def evaluate_review_ack_eligibility(
    blocking_requirements: Sequence[ReviewAckRequirement],
) -> ReviewAckEligibilityDecision:
    blocked = tuple(blocking_requirements)
    if blocked:
        return ReviewAckEligibilityDecision(
            eligible=False,
            reason=REVIEW_FINDING_ACK_REQUIRED,
            status_code=409,
            blocked_requirements=blocked,
        )
    return ReviewAckEligibilityDecision(
        eligible=True,
        reason=REVIEW_FINDING_ACK_ELIGIBLE,
        status_code=None,
        blocked_requirements=(),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finding_category(finding: ReviewFinding) -> str:
    return getattr(finding.category, "value", str(finding.category))


def _finding_severity(finding: ReviewFinding) -> str:
    return getattr(finding.severity, "value", str(finding.severity))


def review_findings_requiring_ack(
    review: ChunkReviewRecord | None,
    current_diff_hash: str | None,
) -> tuple[ReviewFinding, ...]:
    """
    Pure A1 selector for current delivered reviewer findings.

    Missing, non-completed, stale, or indeterminate reviews return an empty set.
    The default branch is always "no findings"; only a current completed review
    with high findings in the policy categories can require acknowledgement.
    """
    if review is None:
        return ()
    if review.review_status != ChunkReviewStatus.COMPLETED:
        return ()
    if (
        classify_review_staleness(review, current_diff_hash)
        != ReviewStalenessStatus.CURRENT
    ):
        return ()

    required_categories = {
        str(category) for category in policy.REVIEW_ACK_REQUIRED_CATEGORIES
    }
    required_severity = str(policy.REVIEW_ACK_REQUIRED_SEVERITY)
    return tuple(
        finding
        for finding in review.findings
        if _finding_severity(finding) == required_severity
        and _finding_category(finding) in required_categories
    )


def review_acknowledgement_state(
    run_id: str,
    chunk_number: int,
    current_diff_hash: str | None,
    *,
    requires_ack: bool,
) -> str:
    """
    Classify a review acknowledgement at a chunk's current diff identity.

      - no current A1 finding -> not_required
      - active ack bound to current hash -> current
      - active ack exists for another hash -> stale
      - required but no usable active ack -> missing
    """
    if not requires_ack:
        return REVIEW_ACK_NOT_REQUIRED
    if get_active_acknowledgement(run_id, chunk_number, current_diff_hash):
        return REVIEW_ACK_CURRENT
    init_db()
    with engine.connect() as conn:
        any_active = _latest_active_ack_row(conn, run_id, chunk_number)
    return REVIEW_ACK_STALE if any_active is not None else REVIEW_ACK_MISSING


def review_ack_display_state(
    run_id: str,
    chunk_number: int,
    review: ChunkReviewRecord,
    current_diff_hash: str | None,
) -> tuple[bool, str, int, tuple[str, ...]]:
    """
    Display helper for one loaded review row.

    This performs no review lookup. Callers already have the review/current hash.
    It may read the ack table; read-model callers should catch errors and degrade
    to the safe not-required defaults.
    """
    findings = review_findings_requiring_ack(review, current_diff_hash)
    requires_ack = bool(findings)
    state = review_acknowledgement_state(
        run_id,
        chunk_number,
        current_diff_hash,
        requires_ack=requires_ack,
    )
    return (
        requires_ack,
        state,
        len(findings),
        tuple(sorted({_finding_category(finding) for finding in findings})),
    )


def _latest_active_ack_row(conn, run_id: str, chunk_number: int) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM review_finding_acknowledgements
        WHERE run_id = :run_id
          AND chunk_number = :chunk_number
          AND status = :status
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """), {
        "run_id": run_id,
        "chunk_number": chunk_number,
        "status": REVIEW_ACK_STATUS_ACTIVE,
    }).fetchone()
    return dict(row._mapping) if row else None


def get_active_acknowledgement(
    run_id: str,
    chunk_number: int,
    diff_hash: str | None,
) -> dict | None:
    """Return the active ack for exactly this diff hash, else None."""
    if not diff_hash or not str(diff_hash).strip():
        return None
    init_db()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM review_finding_acknowledgements
            WHERE run_id = :run_id
              AND chunk_number = :chunk_number
              AND status = :status
              AND acknowledged_diff_hash = :diff_hash
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """), {
            "run_id": run_id,
            "chunk_number": chunk_number,
            "status": REVIEW_ACK_STATUS_ACTIVE,
            "diff_hash": str(diff_hash).strip(),
        }).fetchone()
    return dict(row._mapping) if row else None


def create_acknowledgement(
    run_id: str,
    chunk_number: int,
    diff_hash: str,
    *,
    reason: str | None = None,
    acknowledged_by: str | None = None,
) -> dict:
    """
    Record a panel-level review acknowledgement bound to diff_hash.

    Idempotent for (run, chunk, diff hash). The caller must already have proven
    a current A1 finding exists; this layer only validates the binding and writes
    metadata.
    """
    if not diff_hash or not str(diff_hash).strip():
        raise ReviewFindingAckConflictError(
            "Cannot determine the current diff identity for this chunk; refusing "
            "to record an unbound review acknowledgement."
        )
    diff_hash = str(diff_hash).strip()

    try:
        init_db()
        with engine.begin() as conn:
            existing = conn.execute(text("""
                SELECT * FROM review_finding_acknowledgements
                WHERE run_id = :run_id
                  AND chunk_number = :chunk_number
                  AND acknowledged_diff_hash = :diff_hash
                  AND status = :status
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
                "diff_hash": diff_hash,
                "status": REVIEW_ACK_STATUS_ACTIVE,
            }).fetchone()
            if existing is not None:
                return dict(existing._mapping)

            ack_id = str(uuid.uuid4())
            now = _now_iso()
            conn.execute(text("""
                INSERT INTO review_finding_acknowledgements
                (
                    id, run_id, chunk_number, acknowledged_diff_hash,
                    acknowledged_by, acknowledged_at, reason, status, created_at
                )
                VALUES
                (
                    :id, :run_id, :chunk_number, :diff_hash,
                    :acknowledged_by, :acknowledged_at, :reason, :status,
                    :created_at
                )
            """), {
                "id": ack_id,
                "run_id": run_id,
                "chunk_number": chunk_number,
                "diff_hash": diff_hash,
                "acknowledged_by": acknowledged_by,
                "acknowledged_at": now,
                "reason": reason,
                "status": REVIEW_ACK_STATUS_ACTIVE,
                "created_at": now,
            })
            row = conn.execute(text("""
                SELECT * FROM review_finding_acknowledgements WHERE id = :id
            """), {"id": ack_id}).fetchone()
            return dict(row._mapping)
    except ReviewFindingAckConflictError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"review_finding_ack_store.py: create_acknowledgement failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def current_review_requires_ack(
    run_id: str,
    chunk_number: int,
) -> tuple[str, tuple[ReviewFinding, ...]]:
    """
    Strict ack-route helper: prove the current review has A1 findings.

    Raises ReviewFindingAckConflictError when the diff identity is indeterminate
    or when there is no current acknowledgeable review. Routes map this to 409.
    """
    current_hash = current_chunk_review_identity(run_id, chunk_number)
    if not current_hash:
        raise ReviewFindingAckConflictError(
            "Cannot determine the current diff identity for this chunk; no "
            "review acknowledgement was recorded."
        )
    review = get_review_by_checkpoint_hash(run_id, chunk_number, current_hash)
    findings = review_findings_requiring_ack(review, current_hash)
    if not findings:
        raise ReviewFindingAckConflictError(
            "This chunk has no current high-severity requirement-mismatch or "
            "security reviewer finding to acknowledge."
        )
    return current_hash, findings


def chunk_requiring_review_ack(
    run_id: str,
    chunk_number: int,
) -> ReviewAckRequirement | None:
    """
    Strict selector for one chunk. It may raise on edge I/O.

    Enforcement routes should call the safe wrappers below or catch exceptions,
    because item 15's route precondition must fail open on any indeterminate
    review lookup.
    """
    current_hash = current_chunk_review_identity(run_id, chunk_number)
    if not current_hash:
        return None
    review = get_review_by_checkpoint_hash(run_id, chunk_number, current_hash)
    findings = review_findings_requiring_ack(review, current_hash)
    if not findings:
        return None
    state = review_acknowledgement_state(
        run_id,
        chunk_number,
        current_hash,
        requires_ack=True,
    )
    if state == REVIEW_ACK_CURRENT:
        return None
    return ReviewAckRequirement(
        chunk_number=chunk_number,
        state=state,
        current_diff_hash=current_hash,
        finding_count=len(findings),
        categories=tuple(sorted({_finding_category(finding) for finding in findings})),
    )


def safe_chunk_requiring_review_ack(
    run_id: str,
    chunk_number: int,
) -> ReviewAckRequirement | None:
    """Fail-open wrapper for one chunk's enforcement selector."""
    try:
        return chunk_requiring_review_ack(run_id, chunk_number)
    except Exception:
        return None


def chunks_requiring_review_ack(run_id: str) -> list[ReviewAckRequirement]:
    """
    Fail-open selector for final approval.

    Any plan/review/ack lookup problem yields an empty blocking set. That is the
    item 15 safety invariant: the reviewer cannot become a denial-of-approval
    vector because uncertainty never returns "blocked."
    """
    try:
        plan = get_chunk_plan_status(run_id)
    except Exception:
        return []

    blocking: list[ReviewAckRequirement] = []
    for chunk in plan.chunks:
        requirement = safe_chunk_requiring_review_ack(
            run_id, chunk.chunk_number
        )
        if requirement is not None:
            blocking.append(requirement)
    return blocking


def finding_to_steer_text(finding: ReviewFinding) -> str:
    """
    Deterministic finding -> steer text formatter for the existing steer route.

    This grants no scope and creates no new steering semantics. The caller still
    sends the resulting text to POST /runs/{run_id}/chunks/{n}/steer, where the
    unchanged dispatcher handles only failed/completed chunks.
    """
    category = _finding_category(finding)
    severity = _finding_severity(finding)
    affected = ", ".join(finding.affected_files) or "the approved chunk files"
    suggested = finding.suggested_human_check.strip()
    parts = [
        f"Address reviewer finding ({severity} {category}): {finding.title.strip()}",
        finding.explanation.strip(),
        f"Affected files noted by reviewer: {affected}.",
    ]
    if suggested:
        parts.append(f"Suggested human check: {suggested}")
    parts.append(
        "Stay within the already approved chunk scope; do not add files or "
        "expand scope unless the existing scope gate requires it."
    )
    return "\n\n".join(parts)
