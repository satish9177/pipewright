"""
checkpoint_store.py
Saves and loads pipeline step checkpoints.
CRITICAL RULE: Never save a checkpoint unless tests_passed is True.
Every checkpoint stores the git hash at time of save.
"""

import uuid
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from backend.db.database import engine


def save_checkpoint(
    run_id: str,
    step: str,
    output: dict,
    handoff_contract: dict,
    git_hash: str,
    tests_passed: bool,
    chunk_number: int = 0
) -> dict:
    """
    Save a checkpoint for a completed pipeline step.
    RAISES if tests_passed is False.
    This rule has zero exceptions.
    """
    if not tests_passed:
        raise ValueError(
            f"checkpoint_store.py: Cannot checkpoint step '{step}' "
            f"- tests_passed is False. Fix tests before checkpointing."
        )

    checkpoint_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO checkpoints
                (id, run_id, step, status, output,
                 handoff_contract, git_commit_hash,
                 tests_passed, chunk_number, created_at)
                VALUES
                (:id, :run_id, :step, 'complete', :output,
                 :handoff, :git_hash, 1, :chunk_number, :now)
            """), {
                "id": checkpoint_id,
                "run_id": run_id,
                "step": step,
                "output": json.dumps(output),
                "handoff": json.dumps(handoff_contract),
                "git_hash": git_hash,
                "chunk_number": chunk_number,
                "now": now
            })
            conn.commit()
        print(f"Checkpoint saved: {step}")
        return {
            "id": checkpoint_id,
            "step": step,
            "git_hash": git_hash,
            "chunk_number": chunk_number,
        }
    except Exception as e:
        raise RuntimeError(f"checkpoint_store.py: save_checkpoint failed: {e}")


def load_last_checkpoint(run_id: str) -> dict | None:
    """
    Load the most recent successful checkpoint for a run.
    Returns None if no checkpoint exists.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM checkpoints
                WHERE run_id = :run_id
                AND status = 'complete'
                AND tests_passed = 1
                ORDER BY created_at DESC
                LIMIT 1
            """), {"run_id": run_id})
            row = result.fetchone()
            if not row:
                return None
            data = dict(row._mapping)
            data["output"] = json.loads(data["output"])
            data["handoff_contract"] = json.loads(data["handoff_contract"])
            return data
    except Exception as e:
        print(f"checkpoint_store.py: load_last_checkpoint failed: {e}")
        return None


def load_step_checkpoint(run_id: str, step: str) -> dict | None:
    """
    Load checkpoint for a specific step in a run.
    Returns None if step was not checkpointed yet.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM checkpoints
                WHERE run_id = :run_id
                AND step = :step
                AND status = 'complete'
                ORDER BY created_at DESC
                LIMIT 1
            """), {"run_id": run_id, "step": step})
            row = result.fetchone()
            if not row:
                return None
            data = dict(row._mapping)
            data["output"] = json.loads(data["output"])
            data["handoff_contract"] = json.loads(data["handoff_contract"])
            return data
    except Exception as e:
        print(f"checkpoint_store.py: load_step_checkpoint failed: {e}")
        return None


def load_chunk_step_checkpoint(
    run_id: str,
    chunk_number: int,
    step: str
) -> dict | None:
    """
    Load a successful checkpoint for a specific chunk and step.
    Returns None if the chunk step was not checkpointed yet.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM checkpoints
                WHERE run_id = :run_id
                AND chunk_number = :chunk_number
                AND step = :step
                AND status = 'complete'
                AND tests_passed = 1
                ORDER BY created_at DESC
                LIMIT 1
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
                "step": step,
            })
            row = result.fetchone()
            if not row:
                return None
            data = dict(row._mapping)
            data["output"] = json.loads(data["output"])
            data["handoff_contract"] = json.loads(data["handoff_contract"])
            return data
    except Exception as e:
        print(f"checkpoint_store.py: load_chunk_step_checkpoint failed: {e}")
        return None
