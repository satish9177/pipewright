"""
chunk_review_store.py
Isolated persistence for advisory per-chunk AI reviews (Adversarial Reviewer Stage
v1; docs/design/adversarial-reviewer-stage.md).

This is the dedicated, additive home for review records — separate from checkpoints
(resume/safety substrate) and from chunks.completion_summary (already polymorphic),
so advisory LLM evidence never couples into the resume path or patch-failure data.

Scope of this module: boring DB CRUD only. It performs NO LLM calls, NO git calls,
NO route calls, and NO repo mutation. It runs no reviewer and changes no approval,
retry, scope, or final-approval behavior. The one read-only reuse is
``compute_chunk_diff_hash`` (#28F) via ``current_chunk_review_identity`` — we reuse
the existing diff identity rather than inventing a new one, so review staleness and
acknowledgement staleness cannot diverge.
"""

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ReviewStalenessStatus,
    classify_review_staleness,
)
from backend.pipeline.test_validation_ack_store import compute_chunk_diff_hash


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row) -> ChunkReviewRecord:
    """Rebuild a validated ChunkReviewRecord from a stored row."""
    mapping = dict(row._mapping)
    findings_raw = mapping.get("findings_json")
    findings = json.loads(findings_raw) if findings_raw else []
    created_at = mapping.get("created_at")
    return ChunkReviewRecord.model_validate({
        "id": mapping["id"],
        "run_id": mapping["run_id"],
        "chunk_number": mapping["chunk_number"],
        "review_status": mapping["review_status"],
        "verdict": mapping.get("verdict"),
        "summary": mapping.get("summary"),
        "findings": findings,
        "test_gap_summary": mapping.get("test_gap_summary"),
        "scope_summary": mapping.get("scope_summary"),
        "security_or_safety_summary": mapping.get("security_or_safety_summary"),
        "recommended_human_action": mapping.get("recommended_human_action"),
        "reviewed_test_checkpoint_hash": mapping.get("reviewed_test_checkpoint_hash"),
        "checkpoint_id": mapping.get("checkpoint_id"),
        "provider": mapping.get("provider"),
        "model": mapping.get("model"),
        "created_at": str(created_at) if created_at is not None else None,
    })


def create_review(record: ChunkReviewRecord) -> ChunkReviewRecord:
    """
    Persist a single review record and return the stored row.

    The record is already validated by ``ChunkReviewRecord`` (a non-completed
    review is provably empty of verdict/findings/summaries). ``id`` and
    ``created_at`` are generated here if blank. Findings/summaries live ONLY in the
    chunk_reviews row — never in checkpoints or completion_summary.
    """
    review_id = record.id or str(uuid.uuid4())
    created_at = record.created_at or _now_iso()
    # mode="json" so enum members serialize to their string values.
    findings_json = json.dumps(
        [finding.model_dump(mode="json") for finding in record.findings]
    )

    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO chunk_reviews
                (
                    id, run_id, chunk_number, review_status, verdict, summary,
                    findings_json, test_gap_summary, scope_summary,
                    security_or_safety_summary, recommended_human_action,
                    reviewed_test_checkpoint_hash, checkpoint_id, provider, model,
                    created_at
                )
                VALUES
                (
                    :id, :run_id, :chunk_number, :review_status, :verdict, :summary,
                    :findings_json, :test_gap_summary, :scope_summary,
                    :security_or_safety_summary, :recommended_human_action,
                    :reviewed_test_checkpoint_hash, :checkpoint_id, :provider, :model,
                    :created_at
                )
            """), {
                "id": review_id,
                "run_id": record.run_id,
                "chunk_number": record.chunk_number,
                "review_status": record.review_status.value,
                "verdict": record.verdict.value if record.verdict else None,
                "summary": record.summary,
                "findings_json": findings_json,
                "test_gap_summary": record.test_gap_summary,
                "scope_summary": record.scope_summary,
                "security_or_safety_summary": record.security_or_safety_summary,
                "recommended_human_action": record.recommended_human_action,
                "reviewed_test_checkpoint_hash": record.reviewed_test_checkpoint_hash,
                "checkpoint_id": record.checkpoint_id,
                "provider": record.provider,
                "model": record.model,
                "created_at": created_at,
            })
            row = conn.execute(text("""
                SELECT * FROM chunk_reviews WHERE id = :id
            """), {"id": review_id}).fetchone()
            return _row_to_record(row)
    except Exception as error:
        raise RuntimeError(
            f"chunk_review_store.py: create_review failed. "
            f"run_id={record.run_id} | chunk_number={record.chunk_number} | "
            f"error={error}"
        )


def get_latest_review(run_id: str, chunk_number: int) -> ChunkReviewRecord | None:
    """
    The most recent review for (run, chunk), or None.

    Selection is deterministic: ordered by ``created_at`` then ``id`` descending, so
    the newest review wins when several exist for the same chunk (e.g. one per
    retry/amendment). Older reviews remain in the table but are never returned here.
    """
    init_db()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM chunk_reviews
            WHERE run_id = :run_id
              AND chunk_number = :chunk_number
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """), {
            "run_id": run_id,
            "chunk_number": chunk_number,
        }).fetchone()
    return _row_to_record(row) if row else None


def get_review_by_checkpoint_hash(
    run_id: str,
    chunk_number: int,
    checkpoint_hash: str | None,
) -> ChunkReviewRecord | None:
    """
    The most recent review for (run, chunk) bound to ``checkpoint_hash`` exactly,
    or None. A blank/None hash never matches (an indeterminate identity cannot be
    confirmed reviewed), mirroring the #28F acknowledgement lookup.
    """
    if not checkpoint_hash or not str(checkpoint_hash).strip():
        return None
    init_db()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM chunk_reviews
            WHERE run_id = :run_id
              AND chunk_number = :chunk_number
              AND reviewed_test_checkpoint_hash = :checkpoint_hash
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """), {
            "run_id": run_id,
            "chunk_number": chunk_number,
            "checkpoint_hash": str(checkpoint_hash).strip(),
        }).fetchone()
    return _row_to_record(row) if row else None


def review_staleness(
    run_id: str,
    chunk_number: int,
    current_hash: str | None,
) -> ReviewStalenessStatus:
    """
    Classify the latest review's staleness against a caller-supplied
    ``current_hash`` (current / stale / missing). Read-only.

    ``current_hash`` is accepted as an argument (rather than computed here) to keep
    this pure with respect to the identity source for this slice; callers can pass
    ``current_chunk_review_identity(run_id, chunk_number)`` to reuse the #28F hash.
    """
    latest = get_latest_review(run_id, chunk_number)
    return classify_review_staleness(latest, current_hash)


def current_chunk_review_identity(run_id: str, chunk_number: int) -> str | None:
    """
    The chunk's current diff/test-checkpoint identity, reusing the existing #28F
    helper (``compute_chunk_diff_hash``). Read-only; returns None when no usable
    test checkpoint exists. We deliberately do NOT define a separate diff-hash
    scheme so review and acknowledgement staleness stay aligned.
    """
    return compute_chunk_diff_hash(run_id, chunk_number)
