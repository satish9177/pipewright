"""
approval_gate.py
CLI-based human approval gate with SQLite-backed decisions.
The pipeline pauses here until a human approves, rejects, or times out.
"""

import uuid
import json
import time
from datetime import datetime, timezone
from sqlalchemy import text
from backend.db.database import engine
from backend.models.handoff import TestResult, ApprovalRequest
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
    test_result: TestResult,
    ai_summary: str,
    risk_level: str
) -> str:
    try:
        gate_id = str(uuid.uuid4())
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO approval_gates
                (id, run_id, step, status, diff, test_results,
                 ai_summary, plain_english_summary, risk_level)
                VALUES
                (:id, :run_id, 'pre-merge', 'pending', :diff,
                 :test_results, :ai_summary, :plain_english_summary,
                 :risk_level)
            """), {
                "id": gate_id,
                "run_id": run_id,
                "diff": diff,
                "test_results": json.dumps(test_result.model_dump()),
                "ai_summary": ai_summary,
                "plain_english_summary": ai_summary,
                "risk_level": risk_level,
            })
            conn.commit()
        print(f"[APPROVAL] Gate created | run_id={run_id} | gate_id={gate_id}")
        return gate_id
    except Exception as error:
        raise RuntimeError(
            f"approval_gate.py: failed to create gate. "
            f"run_id={run_id} | error={error}"
        )


def _display_approval_request(
    gate_id: str,
    run_id: str,
    test_result: TestResult,
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
        print(f"To approve: POST http://localhost:8000/gates/{gate_id}/approve")
        print(f"To reject:  POST http://localhost:8000/gates/{gate_id}/reject")
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
    test_result: TestResult,
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


def request_approval(
    test_result: TestResult,
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

    started = time.time()
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
                    tests_passed=True
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

            elapsed = time.time() - started
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

            if time.time() - last_waiting_log >= WAITING_LOG_INTERVAL_SECONDS:
                minutes = int(elapsed // 60)
                print("[APPROVAL] Still waiting for decision...")
                print(f"[APPROVAL] {minutes} minutes elapsed")
                last_waiting_log = time.time()

            time.sleep(POLL_INTERVAL_SECONDS)
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
