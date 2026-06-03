"""
test_validation_ack_store.py
Persistence + gate evaluation for runtime test-validation acknowledgements (#28F).

This is the durable, audited home for human acknowledgements of a weak/none
runtime test verdict at the final gate (docs/design/stronger-test-validation.md
§7–§10). It mirrors the #27 scope_expansion_store shape: boring CRUD over a small
table, a conflict error the route maps to 409, and read helpers the final-approval
guard consults.

Safety properties this module owns:

  - An acknowledgement is bound to the EXACT diff it was made against
    (``acknowledged_diff_hash``), sourced from the chunk's latest test checkpoint
    git hash (§9). A retry/amendment (#26/#27) re-runs tests, writes a new test
    checkpoint with a new hash, and thereby makes any prior acknowledgement stale.
  - Acknowledgement is only ever a PRECONDITION for final approval. It never
    commits, pushes, creates a PR, marks the run approved, replaces final
    approval, or changes test pass/fail. It does not gate chunk approval.
  - Only ``weak``/``none`` verdicts can be acknowledged. ``strong``/``unknown``
    (and a chunk with no recorded verdict) require nothing — acknowledging them
    is a conflict.

It does NOT classify tests (that is the pure classify_test_run), run retries, or
weaken any existing gate.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from backend.checkpoint.checkpoint_store import load_chunk_step_checkpoint
from backend.db.database import engine, init_db
from backend.pipeline.chunk_store import (
    get_chunk_plan_status,
    get_chunk_test_run_verdict,
)

# Only these runtime verdicts require (and permit) an acknowledgement in v1.
ACK_REQUIRED_VERDICTS = frozenset({"weak", "none"})

ACK_STATUS_ACTIVE = "active"

# Per-chunk acknowledgement state, from the final gate's point of view.
ACK_NOT_REQUIRED = "not_required"  # strong / unknown / no recorded verdict
ACK_CURRENT = "current"            # active ack bound to the current diff hash
ACK_STALE = "stale"               # an ack exists, but for a different diff hash
ACK_MISSING = "missing"           # weak/none with no usable active ack


class TestValidationAckConflictError(Exception):
    """
    Raised when an acknowledgement cannot be created in the requested state
    (verdict not weak/none, or the current diff identity cannot be determined).
    Distinct from ValueError (not found → 404) so the route maps it to HTTP 409.
    Carries no mutation: nothing was written when this is raised.
    """


@dataclass(frozen=True)
class ChunkAckRequirement:
    """One chunk that blocks final approval until acknowledged (#28F guard)."""

    chunk_number: int
    verdict: str
    state: str  # ACK_MISSING or ACK_STALE
    current_diff_hash: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_chunk_diff_hash(run_id: str, chunk_number: int) -> str | None:
    """
    The canonical "current diff identity" for a chunk (§9): the git hash recorded
    on the chunk's latest successful test checkpoint (the post-patch hash the
    verdict was computed against).

    A retry (#26) or scope amendment (#27) re-runs tests and writes a new test
    checkpoint with a new hash, so this value changes and any prior
    acknowledgement bound to the old hash becomes stale. Returns ``None`` when no
    usable hash exists (no test checkpoint, or an empty hash) — the caller treats
    an indeterminate identity conservatively (cannot acknowledge / not current).
    Pure read; never raises.
    """
    checkpoint = load_chunk_step_checkpoint(run_id, chunk_number, "test")
    if not checkpoint:
        return None
    git_hash = checkpoint.get("git_commit_hash")
    if not git_hash or not str(git_hash).strip():
        return None
    return str(git_hash).strip()


def _latest_active_ack_row(conn, run_id: str, chunk_number: int) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM test_validation_acknowledgements
        WHERE run_id = :run_id
          AND chunk_number = :chunk_number
          AND status = :status
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """), {
        "run_id": run_id,
        "chunk_number": chunk_number,
        "status": ACK_STATUS_ACTIVE,
    }).fetchone()
    return dict(row._mapping) if row else None


def get_active_acknowledgement(
    run_id: str,
    chunk_number: int,
    diff_hash: str | None,
) -> dict | None:
    """
    Return the active acknowledgement for (run, chunk) whose
    ``acknowledged_diff_hash`` matches ``diff_hash`` exactly, else ``None``.

    A ``None``/blank ``diff_hash`` never matches: an indeterminate diff identity
    cannot be confirmed acknowledged, so it fails safe to "no active ack".
    """
    if not diff_hash or not str(diff_hash).strip():
        return None
    init_db()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM test_validation_acknowledgements
            WHERE run_id = :run_id
              AND chunk_number = :chunk_number
              AND status = :status
              AND acknowledged_diff_hash = :diff_hash
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """), {
            "run_id": run_id,
            "chunk_number": chunk_number,
            "status": ACK_STATUS_ACTIVE,
            "diff_hash": str(diff_hash).strip(),
        }).fetchone()
    return dict(row._mapping) if row else None


def create_acknowledgement(
    run_id: str,
    chunk_number: int,
    verdict: str,
    diff_hash: str,
    *,
    reason: str | None = None,
    acknowledged_by: str | None = None,
) -> dict:
    """
    Record a human acknowledgement of a weak/none verdict bound to ``diff_hash``.

    Idempotent: a re-acknowledgement of the same (run, chunk, verdict, diff_hash)
    that is already active returns the existing row rather than inserting a
    duplicate (double-submit safety, §7). Raises
    ``TestValidationAckConflictError`` if the verdict is not acknowledgeable or
    the diff hash is missing — mutating nothing.
    """
    if verdict not in ACK_REQUIRED_VERDICTS:
        raise TestValidationAckConflictError(
            f"Verdict '{verdict}' does not require acknowledgement; nothing to "
            "acknowledge."
        )
    if not diff_hash or not str(diff_hash).strip():
        raise TestValidationAckConflictError(
            "Cannot determine the current diff identity for this chunk; refusing "
            "to record an unbound acknowledgement."
        )
    diff_hash = str(diff_hash).strip()

    try:
        init_db()
        with engine.begin() as conn:
            existing = conn.execute(text("""
                SELECT * FROM test_validation_acknowledgements
                WHERE run_id = :run_id
                  AND chunk_number = :chunk_number
                  AND verdict = :verdict
                  AND acknowledged_diff_hash = :diff_hash
                  AND status = :status
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
                "verdict": verdict,
                "diff_hash": diff_hash,
                "status": ACK_STATUS_ACTIVE,
            }).fetchone()
            if existing is not None:
                return dict(existing._mapping)

            ack_id = str(uuid.uuid4())
            now = _now_iso()
            conn.execute(text("""
                INSERT INTO test_validation_acknowledgements
                (
                    id, run_id, chunk_number, verdict, acknowledged_diff_hash,
                    acknowledged_by, acknowledged_at, reason, status, created_at
                )
                VALUES
                (
                    :id, :run_id, :chunk_number, :verdict, :diff_hash,
                    :acknowledged_by, :acknowledged_at, :reason, :status, :created_at
                )
            """), {
                "id": ack_id,
                "run_id": run_id,
                "chunk_number": chunk_number,
                "verdict": verdict,
                "diff_hash": diff_hash,
                "acknowledged_by": acknowledged_by,
                "acknowledged_at": now,
                "reason": reason,
                "status": ACK_STATUS_ACTIVE,
                "created_at": now,
            })
            row = conn.execute(text("""
                SELECT * FROM test_validation_acknowledgements WHERE id = :id
            """), {"id": ack_id}).fetchone()
            return dict(row._mapping)
    except TestValidationAckConflictError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"test_validation_ack_store.py: create_acknowledgement failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def acknowledgement_state(
    run_id: str,
    chunk_number: int,
    verdict: str | None,
    current_diff_hash: str | None,
) -> str:
    """
    Classify the acknowledgement state of a chunk at the final gate. Pure read.

      - not weak/none (or no verdict) -> ACK_NOT_REQUIRED
      - active ack bound to the current diff hash -> ACK_CURRENT
      - an active ack exists but for a different hash -> ACK_STALE
      - weak/none with no usable active ack (incl. indeterminate hash) -> ACK_MISSING
    """
    if verdict not in ACK_REQUIRED_VERDICTS:
        return ACK_NOT_REQUIRED
    if get_active_acknowledgement(run_id, chunk_number, current_diff_hash) is not None:
        return ACK_CURRENT
    init_db()
    with engine.connect() as conn:
        any_active = _latest_active_ack_row(conn, run_id, chunk_number)
    return ACK_STALE if any_active is not None else ACK_MISSING


def chunks_requiring_acknowledgement(run_id: str) -> list[ChunkAckRequirement]:
    """
    The weak/none chunks that BLOCK final approval because they lack an active
    acknowledgement bound to their current diff hash (§6/§22). Empty list means
    final approval is unblocked by #28F.

    A chunk with verdict strong/unknown or no recorded verdict never appears here
    (no acknowledgement required), so legacy runs and strong runs are unaffected.
    Pure read; never mutates.
    """
    plan = get_chunk_plan_status(run_id)
    blocking: list[ChunkAckRequirement] = []
    for chunk in plan.chunks:
        validation = chunk.test_validation
        verdict = validation.verdict if validation is not None else None
        if verdict not in ACK_REQUIRED_VERDICTS:
            continue
        current_hash = compute_chunk_diff_hash(run_id, chunk.chunk_number)
        state = acknowledgement_state(
            run_id, chunk.chunk_number, verdict, current_hash
        )
        if state != ACK_CURRENT:
            blocking.append(ChunkAckRequirement(
                chunk_number=chunk.chunk_number,
                verdict=verdict,
                state=state,
                current_diff_hash=current_hash,
            ))
    return blocking


def get_chunk_verdict(run_id: str, chunk_number: int) -> str | None:
    """Thin convenience: the persisted runtime verdict for a chunk, or None."""
    stored = get_chunk_test_run_verdict(run_id, chunk_number)
    return stored["verdict"] if stored else None
