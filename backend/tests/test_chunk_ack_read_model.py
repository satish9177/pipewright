"""
test_chunk_ack_read_model.py
Tests for the #28G acknowledgement read-model overlaid on the chunk-plan API.

GET /runs/{run_id}/chunks augments each chunk's display-only test_validation
with the #28F gate's acknowledgement state (requires_acknowledgement +
acknowledgement_status) so the frontend can render the acknowledgement
prompt/confirmation and pre-disable final approval. This is read-only: it adds
no enforcement and reuses the gate's own acknowledgement_state, so the UI can
never disagree with what the backend will enforce. Verdicts and diff hashes are
seeded directly — no git, no LLM, no real test execution.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.chunk_store import (
    create_chunked_run,
    save_chunk_test_run_verdict,
    update_chunk_status,
)
from backend.pipeline.test_run_validation import classify_test_run
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit

client = TestClient(app)


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "test_validation_acknowledgements",
                "checkpoints",
                "approval_gates",
                "chunks",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


# Commands whose classify_test_run verdict we rely on (mirrors the #28F tests).
_VERDICT_COMMANDS = {
    "weak": ("python --version", 0, "Python 3.11.5"),
    "none": ("", 0, ""),
    "strong": ("pytest", 0, "===== 5 passed in 0.10s ====="),
    "unknown": ("./scripts/check.sh", 0, "running checks..."),
}


def _make_run(tmp_path, tracked_runs, *, chunk_count=1):
    project = create_project(
        name=f"ackrm-{uuid.uuid4()}",
        repo_path=str(tmp_path),
        test_command="pytest",
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    chunks = [
        ChunkDefinition(
            chunk_number=n,
            title=f"Chunk {n}",
            description="do it",
            files_expected=["a.py"],
            depends_on=[] if n == 1 else [n - 1],
            risk_level="low",
            token_estimate=10,
            requires_human_review=False,
            rationale="r",
        )
        for n in range(1, chunk_count + 1)
    ]
    triage = TriageResult(
        run_id=run_id,
        project_id=project["id"],
        feature_description="x",
        complexity="easy" if chunk_count == 1 else "medium",
        total_chunks=chunk_count,
        chunks=chunks,
        reasoning="r",
    )
    create_chunked_run(run_id, project["id"], "x", triage)
    return run_id, project


def _seed_verdict(run_id, chunk_number, verdict_key):
    command, exit_code, output = _VERDICT_COMMANDS[verdict_key]
    result = classify_test_run(command, exit_code, output)
    assert result.verdict.value == verdict_key
    save_chunk_test_run_verdict(run_id, chunk_number, result)
    update_chunk_status(run_id, chunk_number, "completed")


def _seed_diff_hash(run_id, chunk_number, git_hash):
    save_checkpoint(
        run_id=run_id,
        step="test",
        output={"passed": True},
        handoff_contract={"passed": True},
        git_hash=git_hash,
        tests_passed=True,
        chunk_number=chunk_number,
    )


def _ack(run_id, chunk_number, reason="manually verified"):
    return client.post(
        f"/runs/{run_id}/chunks/{chunk_number}/test-validation/acknowledge",
        json={"reason": reason},
    )


def _chunk_validation(run_id, chunk_number):
    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    for chunk in response.json()["chunks"]:
        if chunk["chunk_number"] == chunk_number:
            return chunk["test_validation"]
    raise AssertionError(f"chunk {chunk_number} not found")


# ==========================================================================
# requires_acknowledgement / acknowledgement_status
# ==========================================================================


@pytest.mark.parametrize("verdict_key", ["weak", "none"])
def test_weak_or_none_missing_ack_requires_acknowledgement(
    tmp_path, tracked_runs, verdict_key
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, verdict_key)
    _seed_diff_hash(run_id, 1, "HASH_A")

    validation = _chunk_validation(run_id, 1)
    assert validation is not None
    assert validation["verdict"] == verdict_key
    assert validation["requires_acknowledgement"] is True
    assert validation["acknowledgement_status"] == "missing"


@pytest.mark.parametrize("verdict_key", ["weak", "none"])
def test_status_is_current_after_acknowledgement(
    tmp_path, tracked_runs, verdict_key
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, verdict_key)
    _seed_diff_hash(run_id, 1, "HASH_A")

    assert _ack(run_id, 1).status_code == 200

    validation = _chunk_validation(run_id, 1)
    assert validation["requires_acknowledgement"] is True
    assert validation["acknowledgement_status"] == "current"


@pytest.mark.parametrize("verdict_key", ["strong", "unknown"])
def test_strong_or_unknown_not_required(tmp_path, tracked_runs, verdict_key):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, verdict_key)
    _seed_diff_hash(run_id, 1, "HASH_A")

    validation = _chunk_validation(run_id, 1)
    assert validation is not None
    assert validation["verdict"] == verdict_key
    assert validation["requires_acknowledgement"] is False
    assert validation["acknowledgement_status"] == "not_required"


def test_no_verdict_chunk_has_null_validation(tmp_path, tracked_runs):
    # A chunk with no recorded verdict surfaces no test_validation object at all,
    # so there is nothing to acknowledge (and the gate never blocks it).
    run_id, _ = _make_run(tmp_path, tracked_runs)
    validation = _chunk_validation(run_id, 1)
    assert validation is None


def test_status_becomes_stale_when_diff_changes_after_ack(
    tmp_path, tracked_runs
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    assert _ack(run_id, 1).status_code == 200

    # A retry/amendment re-runs tests -> new test checkpoint hash. The prior
    # acknowledgement is bound to HASH_A, so it is now stale.
    _seed_diff_hash(run_id, 1, "HASH_B")

    validation = _chunk_validation(run_id, 1)
    assert validation["requires_acknowledgement"] is True
    assert validation["acknowledgement_status"] == "stale"

    # Re-acknowledging against the new diff restores current.
    assert _ack(run_id, 1).status_code == 200
    validation = _chunk_validation(run_id, 1)
    assert validation["acknowledgement_status"] == "current"


def test_read_model_does_not_mutate_or_change_gate(tmp_path, tracked_runs):
    # Reading the chunk plan must not create acknowledgements or otherwise change
    # state: a weak chunk read repeatedly stays "missing" until explicitly acked.
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")

    for _ in range(3):
        validation = _chunk_validation(run_id, 1)
        assert validation["acknowledgement_status"] == "missing"

    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM test_validation_acknowledgements "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).scalar()
    assert count == 0


def test_multi_chunk_mixed_states(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs, chunk_count=3)
    _seed_verdict(run_id, 1, "strong")
    _seed_diff_hash(run_id, 1, "HASH_1")
    _seed_verdict(run_id, 2, "weak")
    _seed_diff_hash(run_id, 2, "HASH_2")
    _seed_verdict(run_id, 3, "none")
    _seed_diff_hash(run_id, 3, "HASH_3")

    assert _ack(run_id, 2).status_code == 200  # acknowledge only chunk 2

    v1 = _chunk_validation(run_id, 1)
    v2 = _chunk_validation(run_id, 2)
    v3 = _chunk_validation(run_id, 3)

    assert v1["requires_acknowledgement"] is False
    assert v1["acknowledgement_status"] == "not_required"
    assert v2["requires_acknowledgement"] is True
    assert v2["acknowledgement_status"] == "current"
    assert v3["requires_acknowledgement"] is True
    assert v3["acknowledgement_status"] == "missing"
