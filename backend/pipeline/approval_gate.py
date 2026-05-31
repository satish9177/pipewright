"""
approval_gate.py
CLI-based human approval gate with SQLite-backed decisions.
The pipeline pauses here until a human approves, rejects, or times out.
"""

import uuid
import json
import asyncio
from datetime import datetime, timezone
from sqlalchemy import text
from backend.db.database import engine
from backend.models.handoff import PipelineTestResult, ApprovalRequest
from backend.checkpoint.checkpoint_store import save_checkpoint

POLL_INTERVAL_SECONDS = 2
TIMEOUT_SECONDS = 30 * 60
WAITING_LOG_INTERVAL_SECONDS = 30
DIFF_DISPLAY_LINES = 50


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row._mapping)


def _get_gate(gate_id: str) -> dict | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM approval_gates
                WHERE id = :id
            """), {"id": gate_id}).fetchone()
            return _row_to_dict(row) if row else None
    except Exception as error:
        raise RuntimeError(f"approval_gate.py: failed to fetch gate: {error}")


def get_gate(gate_id: str) -> dict | None:
    """
    Return a single approval gate by id.
    Called by FastAPI endpoint for display.
    """
    return _get_gate(gate_id)


def _create_gate(
    run_id: str,
    diff: str,
    test_result: PipelineTestResult,
    ai_summary: str,
    risk_level: str
) -> str:
    try:
        gate_id = str(uuid.uuid4())
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO approval_gates
                (id, run_id, step, status, diff, test_results,
                 ai_summary, plain_english_summary, risk_level, created_at)
                VALUES
                (:id, :run_id, 'pre-merge', 'pending', :diff,
                 :test_results, :ai_summary, :plain_english_summary,
                 :risk_level, :created_at)
            """), {
                "id": gate_id,
                "run_id": run_id,
                "diff": diff,
                "test_results": json.dumps(test_result.model_dump()),
                "ai_summary": ai_summary,
                "plain_english_summary": ai_summary,
                "risk_level": risk_level,
                "created_at": _utc_now(),
            })
            conn.commit()
        print(f"[APPROVAL] Gate created | run_id={run_id} | gate_id={gate_id}")
        return gate_id
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create gate. "
            f"run_id={run_id} | error={error}"
        )


def _get_pending_final_gate(run_id: str) -> dict | None:
    try:
        with engine.connect() as conn:
            return _get_pending_final_gate_for_conn(conn, run_id)
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to fetch final gate. "
            f"run_id={run_id} | error={error}"
        )


def _get_final_gate_for_conn(conn, run_id: str) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE run_id = :run_id
          AND approval_type = 'final'
          AND chunk_number = 0
        ORDER BY created_at DESC
        LIMIT 1
    """), {"run_id": run_id}).fetchone()
    return _row_to_dict(row) if row else None


def _get_pending_final_gate_for_conn(conn, run_id: str) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE run_id = :run_id
          AND approval_type = 'final'
          AND chunk_number = 0
          AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
    """), {"run_id": run_id}).fetchone()
    return _row_to_dict(row) if row else None


def _get_chunk_gate_for_conn(
    conn,
    run_id: str,
    chunk_number: int,
) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE run_id = :run_id
          AND approval_type = 'chunk'
          AND chunk_number = :chunk_number
        ORDER BY created_at DESC
        LIMIT 1
    """), {
        "run_id": run_id,
        "chunk_number": chunk_number,
    }).fetchone()
    return _row_to_dict(row) if row else None


def _get_pending_chunk_gate_for_conn(
    conn,
    run_id: str,
    chunk_number: int,
) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE run_id = :run_id
          AND approval_type = 'chunk'
          AND chunk_number = :chunk_number
          AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
    """), {
        "run_id": run_id,
        "chunk_number": chunk_number,
    }).fetchone()
    return _row_to_dict(row) if row else None


def _create_chunk_approval_gate_for_conn(
    conn,
    run_id: str,
    chunk_number: int,
    summary: str | None = None,
    allow_new_attempt: bool = False,
) -> dict:
    existing = _get_chunk_gate_for_conn(conn, run_id, chunk_number)
    if existing:
        status = existing["status"]
        if status == "pending":
            print(
                f"[APPROVAL] Chunk gate already pending | "
                f"run_id={run_id} | chunk={chunk_number} | "
                f"gate_id={existing['id']}"
            )
            return existing
        if not allow_new_attempt or status != "rejected":
            raise RuntimeError(
                f"approval_gate.py: chunk approval gate already decided. "
                f"run_id={run_id} | chunk={chunk_number} | status={status}"
            )

    gate_id = str(uuid.uuid4())
    branch_name = f"pipewright/{run_id[:8]}"
    chunk_summary = summary or (
        f"Chunk approval required for run {run_id}\n\n"
        f"Chunk: {chunk_number}\n"
        f"Branch: {branch_name}\n\n"
        "Tests have passed. Commit is pending human approval."
    )
    conn.execute(text("""
        INSERT INTO approval_gates
        (
            id, run_id, step, status, ai_summary,
            plain_english_summary, risk_level, chunk_number,
            approval_type, created_at
        )
        VALUES
        (
            :id, :run_id, 'chunk-approval', 'pending',
            :summary, :summary, 'high', :chunk_number, 'chunk', :created_at
        )
    """), {
        "id": gate_id,
        "run_id": run_id,
        "summary": chunk_summary,
        "chunk_number": chunk_number,
        "created_at": _utc_now(),
    })
    gate = _row_to_dict(conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE id = :id
    """), {"id": gate_id}).fetchone())
    print(
        f"[APPROVAL] Chunk gate created | "
        f"run_id={run_id} | chunk={chunk_number} | gate_id={gate_id}"
    )
    return gate


def _create_final_approval_gate_for_conn(
    conn,
    run_id: str,
    summary: str | None = None,
) -> dict:
    existing = _get_final_gate_for_conn(conn, run_id)
    if existing:
        status = existing["status"]
        if status == "pending":
            print(
                f"[APPROVAL] Final gate already pending | "
                f"run_id={run_id} | gate_id={existing['id']}"
            )
            return existing
        raise RuntimeError(
            f"approval_gate.py: final approval gate already decided. "
            f"run_id={run_id} | status={status}"
        )

    gate_id = str(uuid.uuid4())
    final_summary = summary or (
        f"Final approval required for chunked run {run_id}"
    )
    conn.execute(text("""
        INSERT INTO approval_gates
        (
            id, run_id, step, status, ai_summary,
            plain_english_summary, risk_level, chunk_number,
            approval_type, created_at
        )
        VALUES
        (
            :id, :run_id, 'final-approval', 'pending',
            :summary, :summary, 'medium', 0, 'final', :created_at
        )
    """), {
        "id": gate_id,
        "run_id": run_id,
        "summary": final_summary,
        "created_at": _utc_now(),
    })
    gate = _row_to_dict(conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE id = :id
    """), {"id": gate_id}).fetchone())
    print(
        f"[APPROVAL] Final gate created | "
        f"run_id={run_id} | gate_id={gate_id}"
    )
    return gate


def create_final_approval_gate(
    run_id: str,
    summary: str | None = None
) -> dict:
    """
    Create or return the pending final approval gate for a chunked run.
    """
    try:
        with engine.begin() as conn:
            return _create_final_approval_gate_for_conn(conn, run_id, summary)
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create final approval gate. "
            f"run_id={run_id} | error={error}"
        )


def create_chunk_approval_gate(
    run_id: str,
    chunk_number: int,
    summary: str | None = None,
) -> dict:
    """
    Create or return the pending approval gate for a high-risk chunk.
    """
    try:
        with engine.begin() as conn:
            return _create_chunk_approval_gate_for_conn(
                conn,
                run_id,
                chunk_number,
                summary,
            )
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create chunk approval gate. "
            f"run_id={run_id} | chunk={chunk_number} | error={error}"
        )


def create_chunk_approval_gate_and_mark_chunk(
    run_id: str,
    chunk_number: int,
    summary: str,
) -> dict:
    """
    Transactionally create/reuse a chunk gate and mark the run as paused.
    """
    try:
        with engine.begin() as conn:
            gate = _create_chunk_approval_gate_for_conn(
                conn,
                run_id,
                chunk_number,
                summary,
                allow_new_attempt=True,
            )
            chunk_result = conn.execute(text("""
                UPDATE chunks
                SET status = 'awaiting_chunk_approval',
                    error_message = NULL
                WHERE run_id = :run_id
                  AND chunk_number = :chunk_number
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            })
            if chunk_result.rowcount == 0:
                raise RuntimeError(
                    f"approval_gate.py: chunk not found for approval pause. "
                    f"run_id={run_id} | chunk={chunk_number}"
                )
            run_result = conn.execute(text("""
                UPDATE pipeline_runs
                SET status = 'awaiting_chunk_approval',
                    current_step = 'chunk_approval',
                    current_chunk_number = :chunk_number
                WHERE id = :run_id
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            })
            if run_result.rowcount == 0:
                raise RuntimeError(
                    f"approval_gate.py: run not found for chunk approval. "
                    f"run_id={run_id}"
                )
            return gate
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create chunk gate and mark chunk. "
            f"run_id={run_id} | chunk={chunk_number} | error={error}"
        )


def create_final_approval_gate_and_mark_run(
    run_id: str,
    summary: str,
    current_chunk_number: int,
) -> dict:
    """
    Transactionally create/reuse the final gate and mark the run as awaiting it.
    """
    try:
        with engine.begin() as conn:
            gate = _create_final_approval_gate_for_conn(conn, run_id, summary)
            result = conn.execute(text("""
                UPDATE pipeline_runs
                SET status = 'awaiting_final_approval',
                    current_step = 'final_approval',
                    current_chunk_number = :current_chunk_number
                WHERE id = :run_id
            """), {
                "run_id": run_id,
                "current_chunk_number": current_chunk_number,
            })
            if result.rowcount == 0:
                raise RuntimeError(
                    f"approval_gate.py: run not found for final approval. "
                    f"run_id={run_id}"
                )
            return gate
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create final gate and mark run. "
            f"run_id={run_id} | error={error}"
        )


def _get_pending_memory_conflict_gate_for_conn(conn, run_id: str) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE run_id = :run_id
          AND approval_type = 'memory_conflict'
          AND chunk_number = 0
          AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
    """), {"run_id": run_id}).fetchone()
    return _row_to_dict(row) if row else None


def _get_approved_memory_conflict_gate_for_conn(conn, run_id: str) -> dict | None:
    row = conn.execute(text("""
        SELECT * FROM approval_gates
        WHERE run_id = :run_id
          AND approval_type = 'memory_conflict'
          AND chunk_number = 0
          AND status = 'approved'
        ORDER BY created_at DESC
        LIMIT 1
    """), {"run_id": run_id}).fetchone()
    return _row_to_dict(row) if row else None


def get_pending_memory_conflict_gate(run_id: str) -> dict | None:
    """Return the pending DB memory-conflict gate for a run, if any."""
    with engine.connect() as conn:
        return _get_pending_memory_conflict_gate_for_conn(conn, run_id)


def get_approved_memory_conflict_gate(run_id: str) -> dict | None:
    """Return the approved DB memory-conflict gate for a run, if any."""
    with engine.connect() as conn:
        return _get_approved_memory_conflict_gate_for_conn(conn, run_id)


def create_memory_conflict_gate_and_mark_run(
    run_id: str,
    summary: str,
    signature: str,
) -> dict:
    """
    Transactionally create (or reuse a matching pending) DB memory-conflict gate and
    mark the run as awaiting memory-conflict approval (#16D-4).

    The conflict ``signature`` is stored in the existing free-text ``test_results``
    column (no schema change). It lets execute/resume honor an override only when the
    current conflict still matches the one the human approved.
    """
    try:
        with engine.begin() as conn:
            existing = _get_pending_memory_conflict_gate_for_conn(conn, run_id)
            if existing and (existing.get("test_results") or "") == signature:
                print(
                    f"[APPROVAL] Memory conflict gate already pending | "
                    f"run_id={run_id} | gate_id={existing['id']}"
                )
                gate = existing
            else:
                gate_id = str(uuid.uuid4())
                conn.execute(text("""
                    INSERT INTO approval_gates
                    (
                        id, run_id, step, status, ai_summary,
                        plain_english_summary, risk_level, chunk_number,
                        approval_type, test_results, created_at
                    )
                    VALUES
                    (
                        :id, :run_id, 'memory-conflict-approval', 'pending',
                        :summary, :summary, 'high', 0, 'memory_conflict',
                        :signature, :created_at
                    )
                """), {
                    "id": gate_id,
                    "run_id": run_id,
                    "summary": summary,
                    "signature": signature,
                    "created_at": _utc_now(),
                })
                gate = _row_to_dict(conn.execute(text("""
                    SELECT * FROM approval_gates
                    WHERE id = :id
                """), {"id": gate_id}).fetchone())
                print(
                    f"[APPROVAL] Memory conflict gate created | "
                    f"run_id={run_id} | gate_id={gate_id}"
                )

            result = conn.execute(text("""
                UPDATE pipeline_runs
                SET status = 'awaiting_memory_conflict_approval',
                    current_step = 'memory_conflict_approval'
                WHERE id = :run_id
            """), {"run_id": run_id})
            if result.rowcount == 0:
                raise RuntimeError(
                    f"approval_gate.py: run not found for memory conflict gate. "
                    f"run_id={run_id}"
                )
            return gate
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create memory conflict gate and mark run. "
            f"run_id={run_id} | error={error}"
        )


def _display_approval_request(
    gate_id: str,
    run_id: str,
    test_result: PipelineTestResult,
    diff: str,
    ai_summary: str,
    risk_level: str
) -> None:
    try:
        diff_lines = diff.splitlines()
        diff_preview = "\n".join(diff_lines[:DIFF_DISPLAY_LINES])
        if len(diff_lines) > DIFF_DISPLAY_LINES:
            diff_preview += (
                f"\n[APPROVAL] Diff truncated to first "
                f"{DIFF_DISPLAY_LINES} lines"
            )

        status = "PASSED" if test_result.passed else "FAILED"
        print("================================================")
        print("[APPROVAL] Pipeline paused - human decision needed")
        print("================================================")
        print(f"Run ID:     {run_id}")
        print(f"Gate ID:    {gate_id}")
        print(f"Feature:    {ai_summary}")
        print(f"Risk level: {risk_level}")
        print("")
        print("TEST RESULTS:")
        print(f"  Status:  {status}")
        print(f"  Total:   {test_result.total_tests}")
        print(f"  Passed:  {test_result.passed_tests}")
        print(f"  Failed:  {test_result.failed_tests}")
        print(f"  Duration: {test_result.duration_seconds:.1f} seconds")
        print("")
        print("DIFF SUMMARY:")
        print(diff_preview)
        print("")
        print("AI SUMMARY:")
        print(ai_summary)
        print("")
        print(f"To approve: POST http://localhost:8001/gates/{gate_id}/approve")
        print(f"To reject:  POST http://localhost:8001/gates/{gate_id}/reject")
        print('            Body: {"reason": "your reason here"}')
        print("")
        print("Waiting for decision... (timeout in 30 minutes)")
        print("================================================")
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to display approval request: {error}"
        )


def _update_gate_timeout(gate_id: str) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                UPDATE approval_gates
                SET status = 'timeout',
                    rejection_reason = 'Approval timed out',
                    decided_at = :decided_at
                WHERE id = :id
                AND status = 'pending'
            """), {"id": gate_id, "decided_at": _utc_now()})
            conn.commit()
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to timeout gate {gate_id}: {error}"
        )


def _build_approval_request(
    gate_id: str,
    run_id: str,
    test_result: PipelineTestResult,
    diff: str,
    ai_summary: str,
    approved: bool,
    reason: str | None = None,
    risk_level: str = "medium"
) -> ApprovalRequest:
    return ApprovalRequest(
        gate_id=gate_id,
        run_id=run_id,
        diff=diff,
        test_results=test_result,
        ai_summary=ai_summary,
        plain_english_summary=ai_summary,
        risk_level=risk_level,
        approved=approved,
        rejection_reason=reason,
    )


async def request_approval(
    test_result: PipelineTestResult,
    run_id: str,
    diff: str,
    ai_summary: str
) -> ApprovalRequest:
    """
    Synchronous. Creates gate record.
    Polls until human decides or timeout.
    Returns ApprovalRequest with decision.
    """
    print(f"[APPROVAL] Starting | run_id={run_id}")
    risk_level = "medium"
    gate_id = _create_gate(run_id, diff, test_result, ai_summary, risk_level)
    _display_approval_request(
        gate_id, run_id, test_result, diff, ai_summary, risk_level
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    last_waiting_log = started

    try:
        while True:
            gate = _get_gate(gate_id)
            if gate is None:
                raise RuntimeError(
                    f"approval_gate.py: gate disappeared while polling: {gate_id}"
                )

            status = gate["status"]
            if status == "approved":
                print(f"[APPROVAL] Approved | run_id={run_id} | gate_id={gate_id}")
                save_checkpoint(
                    run_id=run_id,
                    step="approval",
                    output={"gate_id": gate_id, "approved": True},
                    handoff_contract={"approved": True},
                    git_hash=test_result.run_id,
                    tests_passed=False,
                    step_completed=True,
                )
                print(f"[APPROVAL] Checkpoint saved | run_id={run_id}")
                print(f"[APPROVAL] Complete | run_id={run_id}")
                return _build_approval_request(
                    gate_id,
                    run_id,
                    test_result,
                    diff,
                    ai_summary,
                    approved=True,
                    risk_level=risk_level,
                )

            if status == "rejected":
                reason = gate.get("rejection_reason") or "No reason provided"
                print(
                    f"[APPROVAL] Rejected | run_id={run_id} | "
                    f"gate_id={gate_id} | reason={reason}"
                )
                print(f"[APPROVAL] Complete | run_id={run_id}")
                return _build_approval_request(
                    gate_id,
                    run_id,
                    test_result,
                    diff,
                    ai_summary,
                    approved=False,
                    reason=reason,
                    risk_level=risk_level,
                )

            elapsed = loop.time() - started
            if elapsed >= TIMEOUT_SECONDS:
                _update_gate_timeout(gate_id)
                print("[APPROVAL] Timeout - no decision after 30 minutes")
                print(f"[APPROVAL] Complete | run_id={run_id}")
                return _build_approval_request(
                    gate_id,
                    run_id,
                    test_result,
                    diff,
                    ai_summary,
                    approved=False,
                    reason="Approval timed out",
                    risk_level=risk_level,
                )

            if loop.time() - last_waiting_log >= WAITING_LOG_INTERVAL_SECONDS:
                minutes = int(elapsed // 60)
                print("[APPROVAL] Still waiting for decision...")
                print(f"[APPROVAL] {minutes} minutes elapsed")
                last_waiting_log = loop.time()

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: approval polling failed. "
            f"run_id={run_id} | gate_id={gate_id} | error={error}"
        )


def approve_gate(gate_id: str) -> bool:
    """
    Called by FastAPI endpoint.
    Updates gate status to approved.
    Returns True if gate found and updated.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE approval_gates
                SET status = 'approved',
                    decided_at = :decided_at
                WHERE id = :id
                AND status = 'pending'
            """), {"id": gate_id, "decided_at": _utc_now()})
            conn.commit()
            updated = result.rowcount > 0
        if updated:
            print(f"[APPROVAL] Gate approved | gate_id={gate_id}")
        else:
            print(f"[APPROVAL] Gate not found for approval | gate_id={gate_id}")
        return updated
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: approve failed. gate_id={gate_id} | error={error}"
        )


def reject_gate(gate_id: str, reason: str) -> bool:
    """
    Called by FastAPI endpoint.
    Updates gate status to rejected.
    Returns True if gate found and updated.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE approval_gates
                SET status = 'rejected',
                    rejection_reason = :reason,
                    decided_at = :decided_at
                WHERE id = :id
                AND status = 'pending'
            """), {
                "id": gate_id,
                "reason": reason,
                "decided_at": _utc_now(),
            })
            conn.commit()
            updated = result.rowcount > 0
        if updated:
            print(f"[APPROVAL] Gate rejected | gate_id={gate_id}")
        else:
            print(f"[APPROVAL] Gate not found for rejection | gate_id={gate_id}")
        return updated
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: reject failed. gate_id={gate_id} | error={error}"
        )


def get_pending_gates() -> list[dict]:
    """
    Returns all pending approval gates.
    Called by FastAPI endpoint for display.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM approval_gates
                WHERE status = 'pending'
                ORDER BY rowid DESC
            """)).fetchall()
            return [_row_to_dict(row) for row in rows]
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to list pending gates: {error}"
        )
