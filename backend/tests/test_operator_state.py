"""
Tests for the pure operator_state read-model foundation.

No DB, git, route, filesystem mutation, frontend, or API response shape is
exercised here. The API wiring slice will adapt real read models into this
context later.
"""

from types import SimpleNamespace

import pytest

from backend.pipeline.operator_state import (
    OperatorAction,
    OperatorActionSeverity,
    OperatorDecisionType,
    OperatorSafetyCheckStatus,
    OperatorStateContext,
    OperatorWaitingOn,
    compute_operator_state,
    safety_check,
)

pytestmark = pytest.mark.unit


def _state(**kwargs):
    return compute_operator_state(OperatorStateContext(**kwargs))


def _blocked_ids(state):
    return {action.id for action in state.blocked_actions}


def _check(state, check_id):
    return next(check for check in state.safety_checks if check.id == check_id)


def test_enum_and_model_serialization_uses_wire_values():
    action = OperatorAction(
        id="approve_final",
        label="Approve final",
        intent="Approve the final run result.",
        severity=OperatorActionSeverity.CAUTION,
    )

    data = action.model_dump()

    assert data["severity"] == "caution"

    state = _state(chunk_plan_awaiting_approval=True)
    dumped = state.model_dump()
    assert dumped["waiting_on"] == "human"
    assert dumped["decision_type"] == "progress"
    assert dumped["primary_action"]["id"] == "approve_plan"


def test_progress_state_uses_primary_action():
    state = _state(plan_approved_not_executed=True)

    assert state.decision_type == OperatorDecisionType.PROGRESS.value
    assert state.primary_action is not None
    assert state.primary_action.id == "execute_chunks"
    assert state.neutral_actions == []


def test_risk_decision_uses_neutral_actions_and_no_primary_action():
    state = _state(pending_scope_expansion=True)

    assert state.decision_type == OperatorDecisionType.RISK_DECISION.value
    assert state.primary_action is None
    assert {action.id for action in state.neutral_actions} == {
        "approve_scope_expansion",
        "reject_scope_expansion",
    }


def test_safety_checks_support_all_statuses():
    statuses = {
        OperatorSafetyCheckStatus.PASSED,
        OperatorSafetyCheckStatus.FAILED,
        OperatorSafetyCheckStatus.WEAK,
        OperatorSafetyCheckStatus.NOT_EVALUATED,
        OperatorSafetyCheckStatus.NOT_APPLICABLE,
    }

    serialized = [
        safety_check(f"id_{status.value}", status.value, status, "detail").model_dump()
        for status in statuses
    ]

    assert {item["status"] for item in serialized} == {
        "passed",
        "failed",
        "weak",
        "not_evaluated",
        "not_applicable",
    }


def test_chunk_plan_awaiting_approval_state():
    state = _state(chunk_plan_awaiting_approval=True)

    assert state.title == "Review the chunk plan"
    assert state.waiting_on == OperatorWaitingOn.HUMAN.value
    assert state.primary_action.id == "approve_plan"
    assert "execute_chunks" in _blocked_ids(state)


def test_plan_approved_executable_state():
    state = _state(plan_approved_not_executed=True)

    assert state.title == "Execute approved chunks"
    assert state.primary_action.id == "execute_chunks"
    assert _check(state, "plan_approval").status == "passed"


def test_running_state_has_no_action():
    state = _state(is_running=True)

    assert state.title == "Pipewright is running"
    assert state.waiting_on == OperatorWaitingOn.SYSTEM.value
    assert state.decision_type == OperatorDecisionType.NONE.value
    assert state.primary_action is None


def test_patch_retry_available_state():
    decision = SimpleNamespace(eligible=True)
    state = _state(patch_failure_present=True, patch_retry_decision=decision)

    # Plain-English, user-facing copy (no "patch"/"retryable"/"read state" jargon).
    assert state.title == "Code change could not be applied"
    explanation = state.explanation.lower()
    assert "code change" in explanation
    assert "could not apply" in explanation
    assert "nothing was committed" in explanation
    assert "tests did not run" in explanation

    assert state.primary_action.id == "retry_patch"
    assert state.primary_action.label == "Retry code change"

    # Blocking final approval reads in plain language, not "patch failure unresolved".
    approve_final = next(
        action for action in state.blocked_actions if action.id == "approve_final"
    )
    assert approve_final.blocked_reason == (
        "The requested code change has not been applied yet."
    )

    assert _check(state, "patch").status == "failed"
    assert _check(state, "patch").label == "Code change"


def test_patch_retry_blocked_state():
    decision = SimpleNamespace(eligible=False, reason="dirty_worktree")
    state = _state(patch_failure_present=True, patch_retry_decision=decision)

    assert state.title == "Code change could not be applied — retry unavailable"
    assert state.primary_action is None
    assert "retry_patch" in _blocked_ids(state)
    # Backend reason stays as a secondary/diagnostic detail.
    assert _check(state, "patch").detail == "dirty_worktree"


def test_pending_scope_expansion_suppresses_normal_retry():
    state = _state(
        pending_scope_expansion=True,
        patch_failure_present=True,
        patch_retry_decision=SimpleNamespace(eligible=True),
        stale_patch_failure_present=True,
    )

    assert state.title == "Scope expansion needs review"
    assert state.decision_type == OperatorDecisionType.RISK_DECISION.value
    assert state.primary_action is None
    assert "retry_patch" in _blocked_ids(state)


def test_scope_expansion_rejected_state():
    state = _state(scope_expansion_rejected=True)

    assert state.title == "Scope expansion was rejected"
    assert state.decision_type == OperatorDecisionType.NONE.value
    assert state.out_of_app_instruction is not None
    assert _check(state, "scope").status == "failed"


def test_wrong_branch_during_scope_approval_fails_closed():
    state = _state(
        wrong_branch=True,
        wrong_branch_context="scope_approval",
        pending_scope_expansion=True,
        wrong_branch_detail="Checkout pipewright/run123.",
    )

    assert state.title == "Scope approval is blocked by branch state"
    assert state.primary_action is None
    assert state.out_of_app_instruction == "Checkout pipewright/run123."
    assert _check(state, "branch").status == "failed"


def test_scope_expansion_success_pauses_at_chunk_approval():
    state = _state(recovered_scope_retry_awaiting_chunk_approval=True)

    assert state.title == "Review recovered scoped change"
    assert state.primary_action.id == "approve_chunk"
    assert "approve_final" in _blocked_ids(state)


@pytest.mark.parametrize("verdict", ["weak", "none"])
def test_weak_or_no_test_ack_missing_is_risk_decision_and_blocks_final(verdict):
    state = _state(test_verdict=verdict, test_ack_state="missing")

    assert state.title == "Acknowledge weak validation"
    assert state.decision_type == OperatorDecisionType.RISK_DECISION.value
    assert state.primary_action is None
    assert [action.id for action in state.neutral_actions] == [
        "acknowledge_test_validation"
    ]
    assert "approve_final" in _blocked_ids(state)
    assert _check(state, "tests").status == "weak"


def test_weak_ack_current_allows_final_when_otherwise_available():
    state = _state(
        test_verdict="weak",
        test_ack_state="current",
        final_approval_available=True,
    )

    assert state.title == "Review final result"
    assert state.primary_action.id == "approve_final"
    assert _check(state, "test_acknowledgement").status == "passed"


def test_stale_ack_blocks_final_approval():
    state = _state(test_verdict="weak", test_ack_state="stale")

    assert state.title == "Test acknowledgement is stale"
    assert state.decision_type == OperatorDecisionType.RISK_DECISION.value
    assert "approve_final" in _blocked_ids(state)
    assert _check(state, "test_acknowledgement").status == "failed"


def test_strong_tests_do_not_require_ack_for_final_approval():
    state = _state(test_verdict="strong", final_approval_available=True)

    assert state.title == "Review final result"
    assert state.primary_action.id == "approve_final"
    assert _check(state, "tests").status == "passed"
    assert _check(state, "test_acknowledgement").status == "not_applicable"


def test_chunk_awaiting_approval_state():
    state = _state(chunk_awaiting_approval=True, test_verdict="strong")

    assert state.title == "Review chunk change"
    assert state.primary_action.id == "approve_chunk"
    assert _check(state, "tests").status == "passed"


def test_final_approval_blocked_state():
    state = _state(
        final_approval_blocked=True,
        final_approval_blocked_reason="Chunk 2 is not complete.",
    )

    assert state.title == "Final approval is blocked"
    assert state.primary_action is None
    assert "approve_final" in _blocked_ids(state)
    assert _check(state, "final_approval").detail == "Chunk 2 is not complete."


def test_final_approval_available_state():
    state = _state(final_approval_available=True, test_verdict="strong")

    assert state.title == "Review final result"
    assert state.decision_type == OperatorDecisionType.PROGRESS.value
    assert state.primary_action.id == "approve_final"


def test_memory_conflict_is_risk_decision():
    state = _state(memory_conflict_pending=True)

    assert state.title == "Resolve memory conflict"
    assert state.primary_action is None
    assert {action.id for action in state.neutral_actions} == {
        "approve_memory_conflict",
        "reject_memory_conflict",
    }


def test_local_only_manual_push_state():
    state = _state(local_only_manual_push=True, pr_ready=True, pr_mode="local_only")

    assert state.title == "Manual push required"
    assert state.waiting_on == OperatorWaitingOn.NOBODY.value
    assert state.primary_action is None
    assert state.out_of_app_instruction is not None
    assert _check(state, "pr").status == "not_applicable"


def test_github_cli_pr_not_created_state():
    state = _state(pr_ready=True, pr_mode="github_cli")

    assert state.title == "Create pull request"
    assert state.primary_action.id == "create_pr"
    assert _check(state, "pr").status == "not_evaluated"


def test_pr_created_or_reused_state():
    state = _state(pr_created=True)

    assert state.title == "Pull request is ready"
    assert state.primary_action is None
    assert _check(state, "pr").status == "passed"


def test_terminal_state():
    state = _state(is_terminal=True, terminal_status="failed")

    assert state.title == "Run is complete"
    assert state.is_terminal is True
    assert state.waiting_on == OperatorWaitingOn.NOBODY.value
    assert state.primary_action is None


def test_unknown_state_fails_closed():
    state = _state(unknown=True)

    assert state.title == "Next safe action is unknown"
    assert state.waiting_on == OperatorWaitingOn.HUMAN.value
    assert state.primary_action is None
    assert state.unknown_state_warning is not None
    assert {"retry_patch", "approve_chunk", "approve_final", "create_pr"} <= _blocked_ids(state)


def test_stalled_state_fails_closed():
    state = _state(is_stalled=True)

    assert state.title == "Run may be stalled"
    assert state.decision_type == OperatorDecisionType.NONE.value
    assert state.primary_action is None
    assert state.out_of_app_instruction is not None
    assert _check(state, "stalled").status == "failed"


def test_pending_scope_expansion_beats_stale_old_patch_failure():
    state = _state(
        pending_scope_expansion=True,
        patch_failure_present=True,
        patch_retry_decision=SimpleNamespace(eligible=True),
        stale_patch_failure_present=True,
    )

    assert state.title == "Scope expansion needs review"
    assert state.primary_action is None
    assert {action.id for action in state.neutral_actions} == {
        "approve_scope_expansion",
        "reject_scope_expansion",
    }
    assert "retry_patch" in _blocked_ids(state)
