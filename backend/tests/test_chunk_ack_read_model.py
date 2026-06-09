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
from backend.pipeline.approval_gate import create_final_approval_gate
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    save_chunk_completion_summary,
    save_chunk_test_run_verdict,
    update_chunk_status,
)
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
    patch_failure_report_to_completion_summary,
    record_initial_attempt,
)
from backend.pipeline.scope_expansion_store import create_scope_expansion_request
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
                "scope_expansion_requests",
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


def _operator_state(run_id):
    response = client.get(f"/runs/{run_id}/chunks")
    assert response.status_code == 200
    body = response.json()
    assert "chunks" in body
    assert "chunk_plan_status" in body
    assert "operator_state" in body
    return body["operator_state"]


def _set_run_status(run_id, status):
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pipeline_runs
                SET status = :status,
                    current_step = :status
                WHERE id = :run_id
            """),
            {"run_id": run_id, "status": status},
        )


def _seed_patch_failure(run_id, *, failure_type=PatchFailureType.PATCH_DOES_NOT_APPLY):
    report = build_patch_failure_report(
        failure_type,
        changed_files_attempted=["a.py"],
        changed_files_actual=[],
        allowed_files=["a.py"],
        working_tree_clean=True,
        chunk_number=1,
    )
    report = record_initial_attempt(
        report,
        failure_report_id="failure-report-1",
        attempt_id="attempt-1",
        started_at="2026-01-01T00:00:00+00:00",
    )
    save_chunk_completion_summary(
        run_id,
        1,
        patch_failure_report_to_completion_summary(report),
    )
    update_chunk_status(run_id, 1, "failed")
    _set_run_status(run_id, "failed")
    return report


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


# ==========================================================================
# operator_state additive read model
# ==========================================================================


def test_operator_state_present_and_previous_fields_remain(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)

    response = client.get(f"/runs/{run_id}/chunks")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["chunk_plan_status"] == "awaiting_approval"
    assert isinstance(body["chunks"], list)
    assert body["operator_state"]["title"] == "Review the chunk plan"


def test_operator_state_chunk_plan_awaiting_approval(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)

    state = _operator_state(run_id)

    assert state["decision_type"] == "progress"
    assert state["primary_action"]["id"] == "approve_plan"


def test_operator_state_plan_approved_not_executed(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    approve_chunk_plan(run_id)

    state = _operator_state(run_id)

    assert state["title"] == "Execute approved chunks"
    assert state["primary_action"]["id"] == "execute_chunks"


def test_operator_state_running_has_no_action(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _set_run_status(run_id, "running_chunks")

    state = _operator_state(run_id)

    assert state["title"] == "Pipewright is running"
    assert state["waiting_on"] == "system"
    assert state["primary_action"] is None


def test_operator_state_patch_retry_available(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    approve_chunk_plan(run_id)
    _seed_patch_failure(run_id)

    state = _operator_state(run_id)

    # #40B: the seeded failure is PATCH_DOES_NOT_APPLY, so the run-level title is
    # the failure-type-aware "could not be applied" family copy (threaded through
    # the route from the persisted report), not the old generic string.
    assert state["title"] == "Code change couldn't be applied"
    assert state["primary_action"]["id"] == "retry_patch"


def test_operator_state_test_failure_after_apply_is_test_specific(tmp_path, tracked_runs):
    # #40B regression through the route: a TEST_FAILURE_AFTER_APPLY failure must
    # surface as a test failure (not an apply failure), never claim tests did not
    # run, and stay non-retryable. Confirms failure_type is threaded end-to-end.
    run_id, _ = _make_run(tmp_path, tracked_runs)
    approve_chunk_plan(run_id)
    _seed_patch_failure(
        run_id, failure_type=PatchFailureType.TEST_FAILURE_AFTER_APPLY
    )

    state = _operator_state(run_id)

    assert state["title"] == "Tests failed after the change was applied"
    assert state["primary_action"] is None
    explanation = state["explanation"].lower()
    assert "rolled back" in explanation
    assert "tests did not run" not in explanation
    tests = next(c for c in state["safety_checks"] if c["id"] == "tests")
    assert tests["status"] == "failed"


def test_operator_state_pending_scope_expansion_is_risk_decision(
    tmp_path,
    tracked_runs,
):
    run_id, project = _make_run(tmp_path, tracked_runs)
    approve_chunk_plan(run_id)
    _seed_patch_failure(run_id, failure_type=PatchFailureType.SCOPE_VIOLATION)
    create_scope_expansion_request(
        run_id,
        project["id"],
        1,
        "failure-report-1",
        requested_files=["src/extra.py"],
    )

    state = _operator_state(run_id)

    assert state["title"] == "Scope expansion needs review"
    assert state["decision_type"] == "risk_decision"
    assert state["primary_action"] is None
    assert {action["id"] for action in state["neutral_actions"]} == {
        "approve_scope_expansion",
        "reject_scope_expansion",
    }
    blocked = {action["id"] for action in state["blocked_actions"]}
    assert "retry_patch" in blocked


def test_operator_state_weak_ack_missing_blocks_final(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    state = _operator_state(run_id)

    assert state["title"] == "Acknowledge weak validation"
    assert state["decision_type"] == "risk_decision"
    assert state["primary_action"] is None
    assert state["neutral_actions"][0]["id"] == "acknowledge_test_validation"


def test_operator_state_current_ack_allows_final(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    assert _ack(run_id, 1).status_code == 200
    create_final_approval_gate(run_id, "final summary")

    state = _operator_state(run_id)

    assert state["title"] == "Review final result"
    assert state["primary_action"]["id"] == "approve_final"


def test_operator_state_stale_ack_blocks_final(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "weak")
    _seed_diff_hash(run_id, 1, "HASH_A")
    assert _ack(run_id, 1).status_code == 200
    _seed_diff_hash(run_id, 1, "HASH_B")
    create_final_approval_gate(run_id, "final summary")

    state = _operator_state(run_id)

    assert state["title"] == "Test acknowledgement is stale"
    assert state["decision_type"] == "risk_decision"
    assert state["primary_action"] is None


def test_operator_state_strong_tests_do_not_require_ack(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _seed_verdict(run_id, 1, "strong")
    _seed_diff_hash(run_id, 1, "HASH_A")
    create_final_approval_gate(run_id, "final summary")

    state = _operator_state(run_id)

    assert state["title"] == "Review final result"
    assert state["primary_action"]["id"] == "approve_final"


def test_operator_state_chunk_awaiting_approval(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    update_chunk_status(run_id, 1, "awaiting_chunk_approval")
    _set_run_status(run_id, "awaiting_chunk_approval")

    state = _operator_state(run_id)

    assert state["title"] == "Review chunk change"
    assert state["primary_action"]["id"] == "approve_chunk"


def test_operator_state_memory_conflict(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    _set_run_status(run_id, "awaiting_memory_conflict_approval")

    state = _operator_state(run_id)

    assert state["title"] == "Resolve memory conflict"
    assert state["decision_type"] == "risk_decision"
    assert {action["id"] for action in state["neutral_actions"]} == {
        "approve_memory_conflict",
        "reject_memory_conflict",
    }


def test_operator_state_pr_created_or_reused(tmp_path, tracked_runs):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pipeline_runs
                SET status = 'complete',
                    pr_url = 'https://github.com/acme/demo/pull/7',
                    pr_number = 7
                WHERE id = :run_id
            """),
            {"run_id": run_id},
        )

    state = _operator_state(run_id)

    assert state["title"] == "Pull request is ready"
    assert state["primary_action"] is None


def test_operator_state_unknown_when_adapter_cannot_map_run(
    tmp_path,
    tracked_runs,
    monkeypatch,
):
    run_id, _ = _make_run(tmp_path, tracked_runs)
    monkeypatch.setattr(
        "backend.routes.chunks._load_operator_state_run_row",
        lambda _run_id: None,
    )

    state = _operator_state(run_id)

    assert state["title"] == "Next safe action is unknown"
    assert state["primary_action"] is None
    assert state["unknown_state_warning"]
