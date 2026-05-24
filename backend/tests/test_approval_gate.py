"""
test_approval_gate.py
Tests for approval_gate.py
No API calls. No Gemini.
Tests database operations for gate management.
Does not test the polling loop
(that requires human interaction).
"""

import uuid
import pytest
from backend.db.database import init_db
from backend.models.handoff import PipelineTestResult, PatchResult
from backend.pipeline.approval_gate import (
    approve_gate,
    create_final_approval_gate,
    reject_gate,
    get_pending_gates,
)
from sqlalchemy import text
from backend.db.database import engine

pytestmark = pytest.mark.unit


def create_test_gate(run_id: str) -> str:
    """
    Directly insert a gate into DB for testing.
    Returns gate_id.
    """
    gate_id = str(uuid.uuid4())
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO approval_gates
            (id, run_id, step, status, risk_level)
            VALUES (:id, :run_id, 'pre-merge', 'pending', 'low')
        """), {"id": gate_id, "run_id": run_id})
        conn.commit()
    return gate_id


def test_approve_gate_updates_status():
    run_id = str(uuid.uuid4())
    gate_id = create_test_gate(run_id)

    result = approve_gate(gate_id)
    assert result is True

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status FROM approval_gates WHERE id = :id"
        ), {"id": gate_id}).fetchone()
    assert row[0] == "approved"


def test_reject_gate_updates_status():
    run_id = str(uuid.uuid4())
    gate_id = create_test_gate(run_id)

    result = reject_gate(gate_id, "not ready")
    assert result is True

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, rejection_reason FROM approval_gates WHERE id = :id"
        ), {"id": gate_id}).fetchone()
    assert row[0] == "rejected"
    assert row[1] == "not ready"


def test_approve_nonexistent_gate_returns_false():
    result = approve_gate("nonexistent-gate-id")
    assert result is False


def test_reject_nonexistent_gate_returns_false():
    result = reject_gate("nonexistent-gate-id", "reason")
    assert result is False


def test_get_pending_gates_returns_list():
    result = get_pending_gates()
    assert isinstance(result, list)


def test_pending_gate_appears_in_list():
    run_id = str(uuid.uuid4())
    gate_id = create_test_gate(run_id)

    gates = get_pending_gates()
    gate_ids = [g["id"] for g in gates]
    assert gate_id in gate_ids


def test_create_final_approval_gate_creates_pending_final_gate():
    run_id = str(uuid.uuid4())

    gate = create_final_approval_gate(run_id, "final summary")

    assert gate["run_id"] == run_id
    assert gate["approval_type"] == "final"
    assert gate["chunk_number"] == 0
    assert gate["status"] == "pending"
    assert gate["plain_english_summary"] == "final summary"


def test_create_final_approval_gate_is_idempotent():
    run_id = str(uuid.uuid4())

    first = create_final_approval_gate(run_id, "first")
    second = create_final_approval_gate(run_id, "second")

    assert first["id"] == second["id"]
    with engine.connect() as conn:
        count = conn.execute(text("""
            SELECT COUNT(*) FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND status = 'pending'
        """), {"run_id": run_id}).fetchone()[0]
    assert count == 1
