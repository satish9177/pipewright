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
    RunPhase,
    _select_state,
    build_narrative,
    compute_operator_state,
    project_phase,
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
    # An eligible retry only ever happens for the "could not be applied" family
    # (the human-retryable types), so the copy describes an apply failure.
    decision = SimpleNamespace(eligible=True)
    state = _state(
        patch_failure_present=True,
        patch_retry_decision=decision,
        failure_type="PATCH_DOES_NOT_APPLY",
    )

    # Plain-English, user-facing copy (no "patch"/"retryable"/"read state" jargon).
    assert state.title == "Code change couldn't be applied"
    explanation = state.explanation.lower()
    assert "didn't match the current files" in explanation
    assert "nothing was committed" in explanation
    assert "try applying the change again" in explanation

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
    # Tests honestly did not run because the change never applied.
    assert _check(state, "tests").status == "not_evaluated"
    assert "didn't run" in _check(state, "tests").detail.lower()


def test_patch_retry_blocked_state():
    decision = SimpleNamespace(eligible=False, reason="dirty_worktree")
    state = _state(
        patch_failure_present=True,
        patch_retry_decision=decision,
        failure_type="PATCH_DOES_NOT_APPLY",
    )

    # Retry-unavailability is never the headline — the title describes what
    # happened, not what the user cannot do.
    assert state.title == "Code change couldn't be applied"
    assert "retry unavailable" not in state.title.lower()
    assert "cannot retry this automatically" in state.explanation.lower()
    assert state.primary_action is None
    assert "retry_patch" in _blocked_ids(state)
    # #40C: the retry-unavailable reason is humanized — the raw identifier never
    # appears as the blocked-action copy. The patch safety-check detail likewise
    # describes the failure in plain language rather than echoing the raw reason.
    retry_blocked = next(
        action for action in state.blocked_actions if action.id == "retry_patch"
    )
    assert retry_blocked.blocked_reason == (
        "Commit or stash your uncommitted changes before retrying."
    )
    assert "dirty_worktree" not in retry_blocked.blocked_reason
    assert _check(state, "patch").detail == (
        "Pipewright could not apply the generated change."
    )


def _patch_failure_panel(failure_type, *, eligible=False, reason="disallowed_failure_type"):
    decision = SimpleNamespace(eligible=eligible, reason=reason)
    return _state(
        patch_failure_present=True,
        patch_retry_decision=decision,
        failure_type=failure_type,
    )


def test_patch_failure_test_failure_after_apply_is_test_specific():
    # Regression for the smoke bug: TEST_FAILURE_AFTER_APPLY must not read as an
    # apply failure or claim tests did not run, and must not be retryable.
    state = _patch_failure_panel("TEST_FAILURE_AFTER_APPLY")

    assert state.title == "Tests failed after the change was applied"
    assert "could not be applied" not in state.title.lower()
    assert "couldn't be applied" not in state.title.lower()

    explanation = state.explanation.lower()
    assert "applied the change and ran your tests" in explanation
    assert "rolled back" in explanation
    assert "nothing was committed" in explanation
    # Must never claim tests did not run.
    assert "tests did not run" not in explanation
    assert "didn't run" not in explanation

    tests = _check(state, "tests")
    assert tests.status == "failed"
    assert "ran and failed" in tests.detail.lower()

    # Retry must remain unavailable — this type is not human-retryable.
    assert state.primary_action is None
    assert "retry_patch" in _blocked_ids(state)


def test_patch_failure_test_regression_has_regression_narrative():
    state = _patch_failure_panel("TEST_REGRESSION")

    assert state.title == "Tests found a regression"
    visible = _all_visible_copy(state).lower()
    assert "what happened:" in visible
    assert "why:" in visible
    assert "what next:" in visible
    assert "real code regression" in visible
    assert _check(state, "tests").status == "failed"
    assert state.primary_action is None


def test_patch_failure_harness_error_has_harness_narrative_and_retry():
    state = _patch_failure_panel("HARNESS_ERROR", eligible=True)

    assert state.title == "Test harness failed"
    visible = _all_visible_copy(state).lower()
    assert "what happened:" in visible
    assert "why:" in visible
    assert "what next:" in visible
    assert "timed out" in visible
    assert _check(state, "tests").status == "failed"
    assert state.primary_action.id == "retry_patch"


@pytest.mark.parametrize(
    "failure_type, expected_title",
    [
        ("PATCH_DOES_NOT_APPLY", "Code change couldn't be applied"),
        ("PATCH_MALFORMED", "Code change couldn't be applied"),
        ("PATCH_PARTIAL_APPLY_BLOCKED", "Code change couldn't be applied"),
        ("TARGET_MISSING", "Code change couldn't be applied"),
        ("STALE_INDEX_OR_FILE_CHANGED", "Code change couldn't be applied"),
        ("TEST_REGRESSION", "Tests found a regression"),
        ("HARNESS_ERROR", "Test harness failed"),
        ("SCOPE_VIOLATION", "Change was blocked — outside approved scope"),
        ("FORBIDDEN_FILE", "Change was blocked — protected file"),
        ("NO_CHANGES", "No change was produced"),
        ("DIRTY_WORKTREE", "Repo wasn't ready for this change"),
        ("UNKNOWN_PATCH_FAILURE", "Something went wrong applying this change"),
    ],
)
def test_patch_failure_family_titles(failure_type, expected_title):
    state = _patch_failure_panel(failure_type)

    assert state.title == expected_title
    # Headlines never expose the raw enum or retry-availability.
    assert failure_type not in state.title
    assert "retry unavailable" not in state.title.lower()
    # Every family states nothing was committed and never claims tests passed.
    assert "nothing was committed" in state.explanation.lower()
    assert _check(state, "tests").status != "passed"
    assert _check(state, "patch").status == "failed"


def test_patch_failure_unknown_type_falls_back_to_unexpected():
    # An unrecognised/new backend type must render the safe generic copy, never
    # a raw enum and never a test-passed claim.
    state = _patch_failure_panel("SOME_NEW_FAILURE_TYPE")

    assert state.title == "Something went wrong applying this change"
    assert "SOME_NEW_FAILURE_TYPE" not in state.title
    assert _check(state, "tests").status != "passed"


def test_patch_failure_missing_type_falls_back_to_unexpected():
    state = _state(
        patch_failure_present=True,
        patch_retry_decision=SimpleNamespace(eligible=False, reason="x"),
        failure_type=None,
    )

    assert state.title == "Something went wrong applying this change"


def test_blocked_for_safety_does_not_offer_plain_retry():
    # Scope/forbidden are safety refusals — they must never offer a plain retry.
    for failure_type in ("SCOPE_VIOLATION", "FORBIDDEN_FILE"):
        state = _patch_failure_panel(failure_type)
        assert state.primary_action is None
        assert "retry_patch" in _blocked_ids(state)


# Stable retry-ineligible reason identifiers from
# backend/pipeline/patch_failures.py (#40C). Each must humanize, and the raw
# token must never appear as primary user-facing copy.
_RETRY_INELIGIBLE_REASONS = [
    "missing_or_malformed_report",
    "missing_failure_report_id",
    "stale_failure_report_id",
    "chunk_not_failed",
    "dependencies_not_met",
    "dirty_worktree",
    "disallowed_failure_type",
    "human_retry_cap_exhausted",
    "wrong_branch",
]


def _all_visible_copy(state) -> str:
    """Every string a user can read in the panel (titles, explanation, action
    labels/intents/reasons, safety-check labels/details) — but NOT internal
    action ids, which are stable machine handles, not prose."""
    parts = [state.title, state.explanation]
    actions = (
        [state.primary_action]
        + list(state.neutral_actions)
        + list(state.secondary_actions)
        + list(state.blocked_actions)
    )
    for action in actions:
        if action is None:
            continue
        parts += [action.label, action.intent, action.blocked_reason or ""]
    for check in state.safety_checks:
        parts += [check.label, check.detail]
    return "\n".join(parts)


@pytest.mark.parametrize("reason", _RETRY_INELIGIBLE_REASONS)
def test_retry_ineligible_reason_is_humanized_not_raw(reason):
    # TEST_FAILURE_AFTER_APPLY is the canonical disallowed-type case, but the
    # humanization must hold for every reason regardless of failure family.
    state = _patch_failure_panel("TEST_FAILURE_AFTER_APPLY", reason=reason)

    visible = _all_visible_copy(state)
    # The raw identifier must never appear as primary user-facing copy.
    assert reason not in visible
    # The blocked retry action carries a plain-language sentence (ends with a
    # period, contains a space — i.e. prose, not a snake_case token).
    retry_blocked = next(
        action for action in state.blocked_actions if action.id == "retry_patch"
    )
    assert retry_blocked.blocked_reason is not None
    assert " " in retry_blocked.blocked_reason
    assert "_" not in retry_blocked.blocked_reason


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("disallowed_failure_type", "This kind of failure cannot be retried automatically."),
        ("human_retry_cap_exhausted", "The retry limit for this failure has already been reached."),
        ("missing_or_malformed_report", "There is no usable failure report to retry from."),
        ("chunk_not_failed", "Retry is only available while the chunk is in a failed state."),
        ("dirty_worktree", "Commit or stash your uncommitted changes before retrying."),
    ],
)
def test_retry_ineligible_reason_specific_copy(reason, expected):
    state = _patch_failure_panel("PATCH_DOES_NOT_APPLY", reason=reason)
    retry_blocked = next(
        action for action in state.blocked_actions if action.id == "retry_patch"
    )
    assert retry_blocked.blocked_reason == expected


def test_retry_ineligible_unknown_reason_falls_back_to_safe_copy():
    state = _patch_failure_panel("PATCH_DOES_NOT_APPLY", reason="some_brand_new_reason")
    retry_blocked = next(
        action for action in state.blocked_actions if action.id == "retry_patch"
    )
    assert "some_brand_new_reason" not in retry_blocked.blocked_reason
    assert retry_blocked.blocked_reason == "This change cannot be retried right now."


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

    # Neutral recovery copy: the flag fires for normal retry recovery too, so it
    # must not claim "scoped"/"expanded-scope" or imply code correctness.
    assert state.title == "Review recovered code change"
    assert "scoped" not in state.title.lower()
    assert "expanded-scope" not in state.explanation.lower()
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


def test_strong_tests_fallback_never_recommends_approve_chunk():
    # #41A: the strong-tests fallback is reached only when no chunk is awaiting
    # approval (that branch outranks this one). So it must NOT recommend
    # approve_chunk — that would be stale/impossible after a chunk completed.
    state = _state(test_verdict="strong")

    assert state.title == "Tests passed with strong validation"
    assert state.primary_action is None
    assert state.decision_type == OperatorDecisionType.NONE.value
    # No stale "approve" action of any kind is offered as the next step.
    assert "approve_chunk" not in {a.id for a in state.blocked_actions}
    assert _check(state, "tests").status == "passed"


def test_chunk_awaiting_approval_state():
    state = _state(chunk_awaiting_approval=True, test_verdict="strong")

    assert state.title == "Review chunk change"
    assert state.primary_action.id == "approve_chunk"
    assert _check(state, "tests").status == "passed"


@pytest.mark.parametrize("verdict", ["weak", "none"])
def test_chunk_approval_outranks_weak_ack_gate(verdict):
    # A chunk awaiting approval while weak/no-test ack is still missing: the
    # immediate next action is to review the chunk, NOT acknowledge validation.
    state = _state(
        chunk_awaiting_approval=True,
        test_verdict=verdict,
        test_ack_state="missing",
    )

    assert state.title == "Review chunk change"
    assert state.decision_type == OperatorDecisionType.PROGRESS.value
    assert state.primary_action.id == "approve_chunk"
    # Weak/no-test signal is not lost: tests still read weak, ack still failed.
    assert _check(state, "tests").status == "weak"
    assert _check(state, "test_acknowledgement").status == "failed"
    # Final approval stays blocked, citing the pending acknowledgement too.
    assert "approve_final" in _blocked_ids(state)
    approve_final = next(
        action for action in state.blocked_actions if action.id == "approve_final"
    )
    assert "acknowledged before final approval" in approve_final.blocked_reason


def test_recovered_chunk_approval_outranks_weak_ack_gate():
    # The recovered/expanded-scope retry awaiting approval behaves identically.
    state = _state(
        recovered_scope_retry_awaiting_chunk_approval=True,
        test_verdict="weak",
        test_ack_state="missing",
    )

    assert state.title == "Review recovered code change"
    assert "scoped" not in state.title.lower()
    assert state.primary_action.id == "approve_chunk"
    assert _check(state, "tests").status == "weak"
    assert _check(state, "test_acknowledgement").status == "failed"
    assert "approve_final" in _blocked_ids(state)


def test_weak_ack_becomes_main_action_once_chunk_approval_done():
    # Once no chunk is awaiting approval, a still-missing weak/no-test ack is
    # the main next action again.
    state = _state(
        chunk_awaiting_approval=False,
        recovered_scope_retry_awaiting_chunk_approval=False,
        test_verdict="weak",
        test_ack_state="missing",
    )

    assert state.title == "Acknowledge weak validation"
    assert [action.id for action in state.neutral_actions] == [
        "acknowledge_test_validation"
    ]
    assert "approve_final" in _blocked_ids(state)


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


def test_pr_created_with_review_attention_does_not_read_as_nothing_needed():
    state = _state(pr_created=True, review_needs_attention=True)

    assert state.title == "Review findings need inspection"
    assert "No further in-app action is required" not in state.explanation
    assert "advisory reviewer findings need human inspection" in state.explanation
    assert state.waiting_on == OperatorWaitingOn.HUMAN.value
    assert state.primary_action is None
    assert _check(state, "pr").status == "passed"
    assert _check(state, "review").status == "weak"


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


# --- Item 16: phase / narrative read-model ----------------------------------
# All display-only. The phase projects the already-selected state onto one of
# six user-facing buckets; the narrative structures the curated copy + computed
# actions into what/why/next. Neither grants authority or is persisted.

_RETRYABLE = SimpleNamespace(eligible=True)
_INELIGIBLE = SimpleNamespace(eligible=False, reason="disallowed_failure_type")
_PUSH_FAILURE = {
    "push_failed": True,
    "push_failure_summary": "Auth failed.",
    "push_failure_next_action": "Re-auth, then retry.",
    "push_failure_retryable": True,
}

# (id, context kwargs, expected phase). Covers every branch compute_operator_state
# can produce — the phase projection must be total over these and never optimistic
# for a failure/anomaly/unknown.
_PHASE_CASES = [
    ("chunk_plan_awaiting_approval", {"chunk_plan_awaiting_approval": True}, RunPhase.WAITING_FOR_YOU),
    ("plan_approved_not_executed", {"plan_approved_not_executed": True}, RunPhase.WAITING_FOR_YOU),
    ("running_pre_execution", {"is_running": True, "run_status": "running"}, RunPhase.PLANNING),
    ("running_chunks", {"is_running": True, "run_status": "running_chunks"}, RunPhase.WORKING),
    ("pushing", {"is_running": True, "run_status": "pushing"}, RunPhase.WORKING),
    (
        "patch_failure_retryable",
        {"patch_failure_present": True, "patch_retry_decision": _RETRYABLE, "failure_type": "PATCH_DOES_NOT_APPLY"},
        RunPhase.NEEDS_ATTENTION,
    ),
    (
        "patch_failure_blocked",
        {"patch_failure_present": True, "patch_retry_decision": _INELIGIBLE, "failure_type": "TEST_REGRESSION"},
        RunPhase.NEEDS_ATTENTION,
    ),
    ("pending_scope_expansion", {"pending_scope_expansion": True}, RunPhase.WAITING_FOR_YOU),
    ("scope_expansion_rejected", {"scope_expansion_rejected": True}, RunPhase.NEEDS_ATTENTION),
    ("memory_conflict", {"memory_conflict_pending": True}, RunPhase.WAITING_FOR_YOU),
    ("chunk_awaiting_approval", {"chunk_awaiting_approval": True, "test_verdict": "strong"}, RunPhase.WAITING_FOR_YOU),
    (
        "recovered_chunk_awaiting_approval",
        {"recovered_scope_retry_awaiting_chunk_approval": True},
        RunPhase.WAITING_FOR_YOU,
    ),
    ("weak_ack_missing", {"test_verdict": "weak", "test_ack_state": "missing"}, RunPhase.WAITING_FOR_YOU),
    ("weak_ack_stale", {"test_verdict": "weak", "test_ack_state": "stale"}, RunPhase.WAITING_FOR_YOU),
    (
        "review_ack_missing_at_chunk_gate",
        {"chunk_awaiting_approval": True, "review_ack_state": "missing"},
        RunPhase.WAITING_FOR_YOU,
    ),
    ("review_ack_missing_at_final", {"review_ack_state": "missing"}, RunPhase.WAITING_FOR_YOU),
    ("final_approval_available", {"final_approval_available": True, "test_verdict": "strong"}, RunPhase.WAITING_FOR_YOU),
    ("final_approval_blocked", {"final_approval_blocked": True}, RunPhase.NEEDS_ATTENTION),
    ("pr_created", {"pr_created": True}, RunPhase.DONE),
    ("push_failed", _PUSH_FAILURE, RunPhase.NEEDS_ATTENTION),
    (
        "local_only_done",
        {"local_only_manual_push": True, "pr_ready": True, "pr_mode": "local_only"},
        RunPhase.DONE,
    ),
    ("pr_ready_github", {"pr_ready": True, "pr_mode": "github_cli"}, RunPhase.WAITING_FOR_YOU),
    ("wrong_branch", {"wrong_branch": True}, RunPhase.NEEDS_ATTENTION),
    ("stalled", {"is_stalled": True}, RunPhase.NEEDS_ATTENTION),
    ("terminal_complete", {"is_terminal": True, "terminal_status": "complete"}, RunPhase.DONE),
    ("terminal_failed", {"is_terminal": True, "terminal_status": "failed"}, RunPhase.NEEDS_ATTENTION),
    ("terminal_rejected", {"is_terminal": True, "terminal_status": "rejected"}, RunPhase.NEEDS_ATTENTION),
    ("terminal_final_rejected", {"is_terminal": True, "terminal_status": "final_rejected"}, RunPhase.NEEDS_ATTENTION),
    ("strong_tests_fallback", {"test_verdict": "strong"}, RunPhase.WORKING),
    ("unknown", {"unknown": True}, RunPhase.NEEDS_ATTENTION),
    ("empty_falls_through_to_unknown", {}, RunPhase.NEEDS_ATTENTION),
]


@pytest.mark.parametrize("case_id, kwargs, expected", _PHASE_CASES, ids=[c[0] for c in _PHASE_CASES])
def test_phase_projection_is_total_and_matches_expected(case_id, kwargs, expected):
    state = _state(**kwargs)
    assert isinstance(state.phase, RunPhase)
    assert state.phase == expected
    # Serializes to the wire string, never the enum repr.
    assert state.model_dump()["phase"] == expected.value


def test_phase_projection_never_optimistic_on_failure_or_unknown():
    # The fail-safe guarantee: a failure/anomaly/unknown is NEVER labelled with a
    # "healthy" phase (Done/Working/Planning/Waiting).
    healthy = {RunPhase.DONE, RunPhase.WORKING, RunPhase.PLANNING, RunPhase.WAITING_FOR_YOU}
    for kwargs in (
        {"unknown": True},
        {},
        {"is_stalled": True},
        {"wrong_branch": True},
        {"scope_expansion_rejected": True},
        {"final_approval_blocked": True},
        _PUSH_FAILURE,
        {"is_terminal": True, "terminal_status": "failed"},
        {"patch_failure_present": True, "patch_retry_decision": _RETRYABLE, "failure_type": "PATCH_DOES_NOT_APPLY"},
    ):
        assert _state(**kwargs).phase not in healthy


def test_unknown_fallback_phase_is_needs_attention():
    state = _state(unknown=True)
    assert state.title == "Next safe action is unknown"
    assert state.phase == RunPhase.NEEDS_ATTENTION


def test_d1_retryable_patch_failure_is_needs_attention_not_waiting():
    # A retryable patch failure offers a PROGRESS retry action, but it is a
    # failure first (D1) — it must not read as an ordinary "waiting for you" gate.
    state = _state(
        patch_failure_present=True,
        patch_retry_decision=_RETRYABLE,
        failure_type="PATCH_DOES_NOT_APPLY",
    )
    assert state.primary_action.id == "retry_patch"
    assert state.decision_type == OperatorDecisionType.PROGRESS.value
    assert state.phase == RunPhase.NEEDS_ATTENTION


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pending_scope_expansion": True},
        {"memory_conflict_pending": True},
        {"test_verdict": "weak", "test_ack_state": "missing"},
        {"review_ack_state": "missing"},
    ],
)
def test_d1_expected_gates_and_acks_are_waiting_for_you(kwargs):
    assert _state(**kwargs).phase == RunPhase.WAITING_FOR_YOU


def test_d1_stalled_is_needs_attention():
    assert _state(is_stalled=True).phase == RunPhase.NEEDS_ATTENTION


def test_d1_planning_vs_working_split():
    assert _state(is_running=True, run_status="running").phase == RunPhase.PLANNING
    assert _state(is_running=True, run_status="running_chunks").phase == RunPhase.WORKING


def test_d1_terminal_success_done_failure_needs_attention():
    assert _state(is_terminal=True, terminal_status="complete").phase == RunPhase.DONE
    for status in ("failed", "rejected", "final_rejected"):
        assert _state(is_terminal=True, terminal_status=status).phase == RunPhase.NEEDS_ATTENTION


def test_phase_matches_displayed_state_on_gate_failure_overlap():
    # When a higher-precedence gate is displayed even though a failure flag is
    # also set (the "scope expansion beats stale patch failure" case), the phase
    # follows the DISPLAYED gate, not the incidental failure flag.
    state = _state(
        pending_scope_expansion=True,
        patch_failure_present=True,
        patch_retry_decision=_RETRYABLE,
        stale_patch_failure_present=True,
    )
    assert state.title == "Scope expansion needs review"
    assert state.phase == RunPhase.WAITING_FOR_YOU


def test_narrative_structures_curated_copy_and_actions():
    state = _state(chunk_plan_awaiting_approval=True)
    narrative = state.narrative
    # what_happened / why reuse the curated copy verbatim (no new prose).
    assert narrative.what_happened == state.title
    assert narrative.why == state.explanation
    # whats_next is the available action labels, in order.
    assert narrative.whats_next == ("Approve chunk plan",)
    # Serializes as a nested object the frontend can render.
    dumped = state.model_dump()["narrative"]
    assert dumped["what_happened"] == state.title
    assert dumped["why"] == state.explanation
    assert list(dumped["whats_next"]) == ["Approve chunk plan"]


def test_narrative_whats_next_includes_co_equal_actions():
    state = _state(pending_scope_expansion=True)
    # Risk decision: both neutral actions are offered as next steps, no primary.
    assert set(state.narrative.whats_next) == {
        "Approve scope expansion and retry",
        "Reject scope expansion",
    }


def _available_labels(state) -> set[str]:
    actions = []
    if state.primary_action is not None:
        actions.append(state.primary_action)
    actions += list(state.neutral_actions) + list(state.secondary_actions)
    return {action.label for action in actions}


@pytest.mark.parametrize("case_id, kwargs, expected", _PHASE_CASES, ids=[c[0] for c in _PHASE_CASES])
def test_narrative_whats_next_is_subset_of_computed_actions(case_id, kwargs, expected):
    state = _state(**kwargs)
    nxt = set(state.narrative.whats_next)
    # No phantom action: every next step is an actually-computed available action.
    assert nxt <= _available_labels(state)
    # No blocked action is ever presented as available.
    blocked = {action.label for action in state.blocked_actions}
    assert nxt.isdisjoint(blocked)


def test_narrative_whats_next_empty_when_no_available_action():
    # Running, terminal, and blocked-only states offer nothing to do in-app.
    for kwargs in ({"is_running": True}, {"is_terminal": True, "terminal_status": "complete"}, {"unknown": True}):
        assert _state(**kwargs).narrative.whats_next == ()


def test_narrative_makes_no_code_correctness_claim():
    # Strong tests are a PROCESS signal only; the narrative must inherit the
    # existing honest disclaimer and never claim the code itself is correct.
    strong = _state(test_verdict="strong")
    assert "does not prove code correctness" in strong.narrative.why.lower()

    forbidden = ("code is correct", "proven correct", "guaranteed correct", "definitely correct")
    for kwargs in (
        {"test_verdict": "strong"},
        {"test_verdict": "strong", "final_approval_available": True},
        {"chunk_awaiting_approval": True, "test_verdict": "strong"},
    ):
        narrative = _state(**kwargs).narrative
        blob = " ".join([narrative.what_happened, narrative.why, *narrative.whats_next]).lower()
        for phrase in forbidden:
            assert phrase not in blob


def test_narrative_does_not_leak_raw_evidence():
    # The narrative is built only from curated title/explanation/action labels;
    # operator_state never ingests raw test output, diffs, or stack traces, so
    # none of those markers can appear in the narrative.
    state = _state(
        patch_failure_present=True,
        patch_retry_decision=_INELIGIBLE,
        failure_type="TEST_REGRESSION",
    )
    narrative = state.narrative
    assert narrative.what_happened == state.title
    assert narrative.why == state.explanation
    blob = " ".join([narrative.what_happened, narrative.why, *narrative.whats_next])
    for marker in ("Traceback", "exit_code=", "--- a/", "+++ b/", "assert ", "failing_test_ids"):
        assert marker not in blob


def test_existing_fields_unchanged_only_phase_and_narrative_added():
    # The slice is additive: every pre-existing field is byte-identical to the
    # pre-item-16 selection; only phase + narrative are new, and schema_version
    # is bumped to 2.
    pre_item16_fields = [
        "title", "explanation", "waiting_on", "decision_type", "primary_action",
        "neutral_actions", "secondary_actions", "blocked_actions", "safety_checks",
        "trust_facts", "out_of_app_instruction", "is_terminal", "unknown_state_warning",
    ]
    for _case_id, kwargs, _expected in _PHASE_CASES:
        context = OperatorStateContext(**kwargs)
        base = _select_state(context)
        full = compute_operator_state(context)
        for name in pre_item16_fields:
            assert getattr(full, name) == getattr(base, name), name
        assert full.schema_version == 2
        assert full.phase is not None
        assert full.narrative is not None
        # The selection helper itself does not populate the additive fields.
        assert base.phase is None
        assert base.narrative is None


def test_model_dump_adds_phase_and_narrative_keys_only():
    dumped = _state(chunk_plan_awaiting_approval=True).model_dump()
    expected_keys = {
        "title", "explanation", "waiting_on", "decision_type", "schema_version",
        "primary_action", "neutral_actions", "secondary_actions", "blocked_actions",
        "safety_checks", "trust_facts", "out_of_app_instruction", "is_terminal",
        "unknown_state_warning", "phase", "narrative",
    }
    assert set(dumped) == expected_keys
    assert dumped["schema_version"] == 2
    assert dumped["phase"] in {p.value for p in RunPhase}
    assert set(dumped["narrative"]) == {"what_happened", "why", "whats_next"}


def test_project_phase_and_build_narrative_are_pure_helpers():
    # The projection reads the selected state + context; the narrative builder
    # reads only the state. Neither mutates its inputs.
    context = OperatorStateContext(final_approval_available=True, test_verdict="strong")
    state = _select_state(context)
    assert project_phase(state, context) == RunPhase.WAITING_FOR_YOU
    narrative = build_narrative(state)
    assert narrative.whats_next == ("Approve final",)
    # State is frozen/unchanged; helpers returned new values, not mutations.
    assert state.phase is None
    assert state.narrative is None
