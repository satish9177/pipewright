"""
Unit tests for the pure patch failure model/helper layer (PR #18B).

These cover the taxonomy, default messages, suggested-action rules, retry-cap
behavior, stale-index hint, the report factory, and completion_summary
(de)serialization. No filesystem, git, DB, or pipeline wiring is exercised.
"""

import pytest

from backend.pipeline.patch_failures import (
    ACTION_MARK_MANUAL_INTERVENTION,
    ACTION_REINDEX,
    ACTION_REJECT_CHUNK,
    ACTION_RETRY,
    ACTION_RETRY_WITH_INSTRUCTION,
    ACTION_VIEW_DETAILS,
    MAX_HUMAN_RETRIES,
    MAX_RECOVERY_ATTEMPTS,
    MAX_TECHNICAL_DETAILS_CHARS,
    PATCH_FAILURE_KIND,
    RECOVERED_PATCH_REVIEW_KIND,
    RETRY_INELIGIBLE_CAP_EXHAUSTED,
    RETRY_INELIGIBLE_CHUNK_NOT_FAILED,
    RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET,
    RETRY_INELIGIBLE_DIRTY_WORKTREE,
    RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE,
    RETRY_INELIGIBLE_MISSING_FAILURE_REPORT_ID,
    RETRY_INELIGIBLE_MISSING_REPORT,
    RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID,
    RETRY_INELIGIBLE_WRONG_BRANCH,
    PatchFailureReport,
    PatchFailureType,
    PatchRecoveryAttempt,
    PatchRetryEligibilityDecision,
    RecoveredPatchReviewSummary,
    build_patch_failure_report,
    count_human_retry_attempts,
    default_message_for_failure_type,
    evaluate_patch_retry_eligibility,
    human_retry_ineligible_reason,
    patch_failure_report_from_completion_summary,
    patch_failure_report_to_completion_summary,
    record_initial_attempt,
    record_retry_attempt,
    recovered_patch_review_to_completion_summary,
    stale_index_hint_for,
    suggested_actions_for,
)
from backend.pipeline.patch_applier import classify_patch_failure

pytestmark = pytest.mark.unit


EXPECTED_FAILURE_TYPES = {
    "PATCH_MALFORMED",
    "PATCH_DOES_NOT_APPLY",
    "PATCH_PARTIAL_APPLY_BLOCKED",
    "SCOPE_VIOLATION",
    "FORBIDDEN_FILE",
    "TARGET_MISSING",
    "STALE_INDEX_OR_FILE_CHANGED",
    "NO_CHANGES",
    "TEST_FAILURE_AFTER_APPLY",
    "TEST_REGRESSION",
    "HARNESS_ERROR",
    "DIRTY_WORKTREE",
    "UNKNOWN_PATCH_FAILURE",
}


# --------------------------------------------------------------------------- #
# Enum + default messages
# --------------------------------------------------------------------------- #


def test_enum_has_exactly_the_expected_closed_set():
    assert {member.value for member in PatchFailureType} == EXPECTED_FAILURE_TYPES
    # str Enum: members compare/serialize as their string value.
    assert PatchFailureType.SCOPE_VIOLATION == "SCOPE_VIOLATION"


def test_default_message_for_every_type_is_non_empty():
    for member in PatchFailureType:
        message = default_message_for_failure_type(member)
        assert isinstance(message, str)
        assert message.strip()


def test_specific_default_messages_match_design():
    assert default_message_for_failure_type(PatchFailureType.PATCH_MALFORMED) == (
        "The generated change was malformed and could not be read as a patch."
    )
    assert default_message_for_failure_type(
        PatchFailureType.SCOPE_VIOLATION
    ) == (
        "The change tried to edit files outside the approved chunk scope and "
        "was rejected."
    )
    assert default_message_for_failure_type(
        PatchFailureType.DIRTY_WORKTREE
    ).startswith("You have uncommitted changes.")


# --------------------------------------------------------------------------- #
# suggested_actions_for
# --------------------------------------------------------------------------- #


def test_view_details_always_present():
    for member in PatchFailureType:
        actions = suggested_actions_for(member, attempts=0, max_attempts=2)
        assert ACTION_VIEW_DETAILS in actions
        assert ACTION_REJECT_CHUNK in actions
        assert ACTION_MARK_MANUAL_INTERVENTION in actions


def test_patch_malformed_includes_retry_when_under_cap():
    actions = suggested_actions_for(
        PatchFailureType.PATCH_MALFORMED, attempts=0, max_attempts=2
    )
    assert ACTION_RETRY in actions


def test_patch_does_not_apply_includes_retry_reindex_view_details():
    actions = suggested_actions_for(
        PatchFailureType.PATCH_DOES_NOT_APPLY, attempts=0, max_attempts=2
    )
    assert ACTION_RETRY in actions
    assert ACTION_REINDEX in actions
    assert ACTION_VIEW_DETAILS in actions


def test_stale_index_includes_reindex_but_no_plain_retry():
    actions = suggested_actions_for(
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED, attempts=0, max_attempts=2
    )
    assert ACTION_REINDEX in actions
    assert ACTION_RETRY not in actions


def test_scope_violation_has_no_plain_retry():
    actions = suggested_actions_for(
        PatchFailureType.SCOPE_VIOLATION, attempts=0, max_attempts=2
    )
    assert ACTION_RETRY not in actions
    assert ACTION_RETRY_WITH_INSTRUCTION in actions
    assert ACTION_REINDEX not in actions
    assert ACTION_REJECT_CHUNK in actions


def test_forbidden_file_has_no_retry_or_reindex():
    actions = suggested_actions_for(
        PatchFailureType.FORBIDDEN_FILE, attempts=0, max_attempts=2
    )
    assert ACTION_RETRY not in actions
    assert ACTION_RETRY_WITH_INSTRUCTION not in actions
    assert ACTION_REINDEX not in actions
    assert actions == [
        ACTION_REJECT_CHUNK,
        ACTION_MARK_MANUAL_INTERVENTION,
        ACTION_VIEW_DETAILS,
    ]


def test_dirty_worktree_includes_retry():
    # Human-initiated retry after cleaning the tree; offered regardless of the
    # patch retry budget (it is not an auto-retry).
    actions = suggested_actions_for(
        PatchFailureType.DIRTY_WORKTREE, attempts=0, max_attempts=0
    )
    assert ACTION_RETRY in actions
    assert ACTION_REINDEX not in actions


def test_no_changes_has_no_plain_retry():
    actions = suggested_actions_for(
        PatchFailureType.NO_CHANGES, attempts=0, max_attempts=2
    )
    assert ACTION_RETRY not in actions
    assert ACTION_RETRY_WITH_INSTRUCTION in actions
    assert ACTION_REINDEX not in actions


def test_retry_cap_exhausted_removes_retry_keeps_manual_and_reject():
    actions = suggested_actions_for(
        PatchFailureType.PATCH_DOES_NOT_APPLY, attempts=2, max_attempts=2
    )
    assert ACTION_RETRY not in actions
    assert ACTION_MARK_MANUAL_INTERVENTION in actions
    assert ACTION_REJECT_CHUNK in actions
    assert ACTION_VIEW_DETAILS in actions
    # reindex is still meaningful for an apply/stale category after the cap.
    assert ACTION_REINDEX in actions


def test_zero_budget_means_no_auto_retry_for_transient():
    actions = suggested_actions_for(
        PatchFailureType.TARGET_MISSING, attempts=0, max_attempts=0
    )
    assert ACTION_RETRY not in actions


def test_actions_are_returned_in_deterministic_order():
    actions = suggested_actions_for(
        PatchFailureType.PATCH_DOES_NOT_APPLY, attempts=0, max_attempts=2
    )
    assert actions == sorted(
        actions,
        key=lambda a: [
            ACTION_RETRY,
            ACTION_RETRY_WITH_INSTRUCTION,
            ACTION_REINDEX,
            ACTION_REJECT_CHUNK,
            ACTION_MARK_MANUAL_INTERVENTION,
            ACTION_VIEW_DETAILS,
        ].index(a),
    )


# --------------------------------------------------------------------------- #
# stale_index_hint_for
# --------------------------------------------------------------------------- #


def test_stale_index_hint_true_only_for_intended_categories():
    expected_true = {
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED,
        PatchFailureType.TARGET_MISSING,
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED,
    }
    for member in PatchFailureType:
        assert stale_index_hint_for(member) is (member in expected_true)


# --------------------------------------------------------------------------- #
# build_patch_failure_report
# --------------------------------------------------------------------------- #


def test_report_factory_fills_fields():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details="hunk failed at line 41",
        changed_files_attempted=["src/foo.py", "src/bar.py"],
        allowed_files=["src/foo.py", "src/bar.py"],
        attempts=0,
        max_attempts=2,
        chunk_number=3,
    )
    assert report.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY
    assert report.message == default_message_for_failure_type(
        PatchFailureType.PATCH_DOES_NOT_APPLY
    )
    assert report.changed_files_attempted == ["src/foo.py", "src/bar.py"]
    assert report.allowed_files == ["src/foo.py", "src/bar.py"]
    assert report.stale_index_hint is True
    assert report.chunk_number == 3
    assert report.failed_step == "patch"
    assert ACTION_RETRY in report.suggested_actions
    assert report.retry.attempts == 0
    assert report.retry.max_attempts == 2
    assert report.retry.retryable is True


def test_create_target_exists_maps_to_patch_does_not_apply():
    error = RuntimeError(
        "patch_applier.py: create target already exists: "
        "docs/testing/m5-7beta-smoke.md"
    )

    assert classify_patch_failure(error, phase="apply") == (
        PatchFailureType.PATCH_DOES_NOT_APPLY
    )


def test_create_target_exists_report_uses_honest_message_without_stale_guidance():
    detail = (
        "patch_applier.py: create target already exists: "
        "docs/testing/m5-7beta-smoke.md"
    )

    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details=detail,
        changed_files_attempted=["docs/testing/m5-7beta-smoke.md"],
        attempts=0,
        max_attempts=2,
    )

    assert report.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY
    assert report.message == (
        "The change tried to create a file that already exists. The file "
        "should be edited, not created."
    )
    lowered = report.message.lower()
    assert "stale" not in lowered
    assert "re-index" not in lowered
    assert "out-of-date" not in lowered
    assert report.stale_index_hint is False
    assert ACTION_REINDEX not in report.suggested_actions
    assert ACTION_RETRY in report.suggested_actions
    assert report.retry.retryable is True
    assert report.technical_details == detail


def test_create_target_exists_retry_eligibility_is_unchanged():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details=(
            "patch_applier.py: create target already exists: "
            "docs/testing/m5-7beta-smoke.md"
        ),
        attempts=0,
        max_attempts=2,
    ).model_copy(update={"failure_report_id": "frid-1"})

    decision = evaluate_patch_retry_eligibility(
        report,
        requested_failure_report_id="frid-1",
        dependencies_met=True,
        working_tree_clean=True,
        chunk_status="failed",
    )

    assert decision.eligible is True
    assert decision.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY


def test_normal_patch_does_not_apply_keeps_stale_guidance():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        technical_details=(
            "patch_applier.py: edit old_string not found in src/app.py."
        ),
        attempts=0,
        max_attempts=2,
    )

    assert report.message == default_message_for_failure_type(
        PatchFailureType.PATCH_DOES_NOT_APPLY
    )
    assert "out-of-date" in report.message
    assert report.stale_index_hint is True
    assert ACTION_REINDEX in report.suggested_actions
    assert ACTION_RETRY in report.suggested_actions
    assert report.retry.retryable is True


def test_report_preserves_rollback_and_clean_flags():
    report = build_patch_failure_report(
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        rollback_performed=True,
        working_tree_clean=True,
        attempts=0,
        max_attempts=2,
    )
    assert report.rollback_performed is True
    assert report.working_tree_clean is True
    assert report.manual_intervention_needed is False


def test_working_tree_dirty_after_rollback_sets_manual_intervention():
    report = build_patch_failure_report(
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        rollback_performed=True,
        working_tree_clean=False,
        attempts=0,
        max_attempts=2,
    )
    assert report.manual_intervention_needed is True


def test_retry_cap_exhausted_sets_manual_intervention_and_drops_retry():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_MALFORMED,
        attempts=2,
        max_attempts=2,
    )
    assert ACTION_RETRY not in report.suggested_actions
    assert report.retry.retryable is False
    assert report.manual_intervention_needed is True


def test_deterministic_failure_does_not_flag_manual_by_default():
    # FORBIDDEN_FILE is not auto-retryable; with no rollback and a clean tree it
    # must not be auto-escalated to manual_intervention_needed.
    report = build_patch_failure_report(PatchFailureType.FORBIDDEN_FILE)
    assert report.manual_intervention_needed is False
    assert report.retry.retryable is False


def test_factory_copies_input_sequences():
    attempted = ["src/foo.py"]
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        changed_files_attempted=attempted,
        max_attempts=2,
    )
    # Mutating the report's list must not affect the caller's list, and vice
    # versa — the factory copied the input.
    report.changed_files_attempted.append("src/bar.py")
    assert attempted == ["src/foo.py"]


def test_technical_details_is_sanitized():
    report = build_patch_failure_report(
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
        technical_details="boom token=abcdefghijklmnopqrstuvwxyz1234567890ABCDEF",
    )
    assert "abcdefghijklmnopqrstuvwxyz1234567890ABCDEF" not in report.technical_details
    assert "[REDACTED]" in report.technical_details


def test_technical_details_is_truncated():
    report = build_patch_failure_report(
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
        technical_details="x" * (MAX_TECHNICAL_DETAILS_CHARS + 500),
    )
    assert len(report.technical_details) <= MAX_TECHNICAL_DETAILS_CHARS + len(
        "\n[truncated]"
    )
    assert report.technical_details.endswith("[truncated]")


def test_none_technical_details_stays_none():
    report = build_patch_failure_report(PatchFailureType.NO_CHANGES)
    assert report.technical_details is None


# --------------------------------------------------------------------------- #
# completion_summary serialization
# --------------------------------------------------------------------------- #


def test_to_completion_summary_includes_kind_and_string_enum():
    report = build_patch_failure_report(
        PatchFailureType.SCOPE_VIOLATION, max_attempts=2
    )
    data = patch_failure_report_to_completion_summary(report)
    assert data["kind"] == PATCH_FAILURE_KIND
    # Enum serialized as a plain string for safe JSON storage.
    assert data["failure_type"] == "SCOPE_VIOLATION"
    assert isinstance(data["failure_type"], str)
    assert data["retry"]["max_attempts"] == 2


def test_round_trip_serialization_preserves_report():
    report = build_patch_failure_report(
        PatchFailureType.TARGET_MISSING,
        changed_files_attempted=["a.py"],
        attempts=1,
        max_attempts=2,
        chunk_number=7,
    )
    data = patch_failure_report_to_completion_summary(report)
    restored = patch_failure_report_from_completion_summary(data)
    assert isinstance(restored, PatchFailureReport)
    assert restored == report


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not a dict",
        123,
        ["list"],
        {},  # missing kind
        {"kind": "success_summary", "failure_type": "TARGET_MISSING"},
        {"kind": PATCH_FAILURE_KIND, "failure_type": "NOT_A_REAL_TYPE"},
        {"kind": PATCH_FAILURE_KIND},  # missing required fields
    ],
)
def test_from_completion_summary_returns_none_for_invalid(value):
    assert patch_failure_report_from_completion_summary(value) is None


def test_from_completion_summary_parses_valid_dict():
    data = {
        "kind": PATCH_FAILURE_KIND,
        "failure_type": "PATCH_MALFORMED",
        "message": "x",
        "technical_details": None,
        "changed_files_attempted": [],
        "changed_files_actual": [],
        "allowed_files": [],
        "suggested_actions": [ACTION_VIEW_DETAILS],
        "rollback_performed": False,
        "working_tree_clean": False,
        "retry": {"attempts": 0, "max_attempts": 2, "retryable": True},
        "stale_index_hint": False,
        "chunk_number": None,
        "failed_step": "patch",
        "manual_intervention_needed": False,
    }
    report = patch_failure_report_from_completion_summary(data)
    assert isinstance(report, PatchFailureReport)
    assert report.failure_type == PatchFailureType.PATCH_MALFORMED
    assert report.retry.max_attempts == 2


def test_old_test_failure_after_apply_summary_still_parses():
    data = {
        "kind": PATCH_FAILURE_KIND,
        "failure_type": "TEST_FAILURE_AFTER_APPLY",
        "message": "The change applied but the project's tests failed.",
        "technical_details": "1 failed",
        "changed_files_attempted": ["app.py"],
        "changed_files_actual": [],
        "allowed_files": ["app.py"],
        "suggested_actions": [ACTION_VIEW_DETAILS],
        "rollback_performed": True,
        "working_tree_clean": True,
        "retry": {"attempts": 0, "max_attempts": 1, "retryable": False},
        "stale_index_hint": False,
        "chunk_number": 1,
        "failed_step": "test",
        "manual_intervention_needed": False,
    }

    report = patch_failure_report_from_completion_summary(data)

    assert isinstance(report, PatchFailureReport)
    assert report.failure_type == PatchFailureType.TEST_FAILURE_AFTER_APPLY


# --------------------------------------------------------------------------- #
# Recovery attempt history / diagnostics (#26C)
# --------------------------------------------------------------------------- #


def _valid_old_failure_dict() -> dict:
    """A stored failure summary from BEFORE #26C (no failure_report_id/attempts)."""
    return {
        "kind": PATCH_FAILURE_KIND,
        "failure_type": "PATCH_MALFORMED",
        "message": "x",
        "technical_details": None,
        "changed_files_attempted": [],
        "changed_files_actual": [],
        "allowed_files": [],
        "suggested_actions": [ACTION_VIEW_DETAILS],
        "rollback_performed": False,
        "working_tree_clean": False,
        "retry": {"attempts": 0, "max_attempts": 2, "retryable": True},
        "stale_index_hint": False,
        "chunk_number": None,
        "failed_step": "patch",
        "manual_intervention_needed": False,
    }


def test_report_defaults_have_no_failure_report_id_or_attempts():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY, max_attempts=2
    )
    assert report.failure_report_id is None
    assert report.attempts == []


def test_old_failure_summary_without_new_fields_still_parses():
    report = patch_failure_report_from_completion_summary(_valid_old_failure_dict())
    assert isinstance(report, PatchFailureReport)
    assert report.failure_report_id is None
    assert report.attempts == []


def test_success_shaped_summary_without_kind_returns_none():
    # A success summary has no `kind`; it must not parse as a patch failure.
    assert patch_failure_report_from_completion_summary(
        {"files_created": ["a.py"], "summary": "did a thing"}
    ) is None


def test_record_initial_attempt_creates_attempt_one():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        changed_files_attempted=["src/a.py"],
        changed_files_actual=[],
        allowed_files=["src/a.py"],
        rollback_performed=True,
        working_tree_clean=True,
        max_attempts=2,
    )
    enriched = record_initial_attempt(
        report,
        failure_report_id="frid-1",
        attempt_id="att-1",
        started_at="2026-06-03T00:00:00+00:00",
    )

    assert enriched.failure_report_id == "frid-1"
    assert len(enriched.attempts) == 1
    attempt = enriched.attempts[0]
    assert attempt.attempt_id == "att-1"
    assert attempt.attempt_number == 1
    assert attempt.started_at == "2026-06-03T00:00:00+00:00"
    assert attempt.recovery_mode == "initial"
    assert attempt.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY
    assert attempt.failed_step == "patch"
    assert attempt.changed_files_attempted == ["src/a.py"]
    assert attempt.changed_files_actual == []
    assert attempt.scope_ok is True
    assert attempt.preimage_matched is None
    assert attempt.model_used is None
    assert attempt.test_outcome == "not_run"
    assert attempt.outcome == "failed"
    assert attempt.human_decision is None
    assert attempt.working_tree_clean is True
    assert attempt.rollback_performed is True

    # Pure: the original report is not mutated.
    assert report.failure_report_id is None
    assert report.attempts == []


def test_record_initial_attempt_scope_violation_sets_scope_ok_false():
    report = build_patch_failure_report(
        PatchFailureType.SCOPE_VIOLATION, max_attempts=2
    )
    enriched = record_initial_attempt(
        report, failure_report_id="f", attempt_id="a", started_at="t"
    )
    assert enriched.attempts[0].scope_ok is False


def test_record_initial_attempt_outcome_manual_intervention():
    # Rollback ran but tree not clean -> manual_intervention_needed -> outcome.
    report = build_patch_failure_report(
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        rollback_performed=True,
        working_tree_clean=False,
        max_attempts=2,
    )
    assert report.manual_intervention_needed is True
    enriched = record_initial_attempt(
        report, failure_report_id="f", attempt_id="a", started_at="t"
    )
    assert enriched.attempts[0].outcome == "manual_intervention"


def test_record_initial_attempt_test_failure_sets_test_outcome():
    report = build_patch_failure_report(
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        rollback_performed=True,
        working_tree_clean=True,
        max_attempts=2,
    )
    enriched = record_initial_attempt(
        report, failure_report_id="f", attempt_id="a", started_at="t"
    )
    assert enriched.attempts[0].test_outcome == "failed"
    # Tree clean + not cap-exhausted -> not manual.
    assert enriched.attempts[0].outcome == "failed"


def test_record_initial_attempt_caps_attempts():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY, max_attempts=2
    )
    prefilled = [
        PatchRecoveryAttempt(
            attempt_id=f"a{i}", attempt_number=i + 1, started_at="t"
        )
        for i in range(MAX_RECOVERY_ATTEMPTS)
    ]
    report = report.model_copy(update={"attempts": prefilled})

    enriched = record_initial_attempt(
        report, failure_report_id="f", attempt_id="new", started_at="t"
    )
    assert len(enriched.attempts) == MAX_RECOVERY_ATTEMPTS
    # Newest appended, oldest ("a0") dropped.
    assert enriched.attempts[-1].attempt_id == "new"
    assert all(a.attempt_id != "a0" for a in enriched.attempts)


def test_round_trip_with_failure_report_id_and_attempts():
    report = build_patch_failure_report(
        PatchFailureType.TARGET_MISSING,
        changed_files_attempted=["a.py"],
        max_attempts=2,
        chunk_number=7,
    )
    enriched = record_initial_attempt(
        report,
        failure_report_id="frid",
        attempt_id="att",
        started_at="2026-06-03T00:00:00+00:00",
    )
    data = patch_failure_report_to_completion_summary(enriched)

    assert data["kind"] == PATCH_FAILURE_KIND
    assert data["failure_report_id"] == "frid"
    assert isinstance(data["attempts"], list)
    assert len(data["attempts"]) == 1

    restored = patch_failure_report_from_completion_summary(data)
    assert isinstance(restored, PatchFailureReport)
    assert restored == enriched


def test_invalid_attempt_data_fails_safe():
    data = _valid_old_failure_dict()
    data["failure_report_id"] = "frid"
    # Attempt missing required fields (attempt_id/attempt_number/started_at).
    data["attempts"] = [{"recovery_mode": "initial"}]
    assert patch_failure_report_from_completion_summary(data) is None


def test_serialized_attempts_have_no_sensitive_keys_or_values():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        changed_files_attempted=["src/a.py"],
        changed_files_actual=["src/a.py"],
        allowed_files=["src/a.py"],
        max_attempts=2,
    )
    enriched = record_initial_attempt(
        report, failure_report_id="frid", attempt_id="att", started_at="t"
    )
    data = patch_failure_report_to_completion_summary(enriched)

    import json

    attempts_blob = json.dumps(data["attempts"])
    for forbidden in ("old_string", "new_string", "content", "token", "secret"):
        assert forbidden not in attempts_blob

    # Attempt keys are exactly the diagnostic set — no content/edit-text fields.
    attempt_keys = set(data["attempts"][0].keys())
    expected_keys = {
        "attempt_id", "attempt_number", "started_at", "recovery_mode",
        "failure_type", "failed_step", "changed_files_attempted",
        "changed_files_actual", "scope_ok", "preimage_matched", "model_used",
        "test_outcome", "outcome", "human_decision", "working_tree_clean",
        "rollback_performed",
    }
    assert attempt_keys == expected_keys


def test_suggested_actions_and_manual_intervention_unchanged_by_enrichment():
    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY, attempts=0, max_attempts=2
    )
    enriched = record_initial_attempt(
        report, failure_report_id="f", attempt_id="a", started_at="t"
    )
    # Enrichment must not change derived behavior.
    assert enriched.suggested_actions == report.suggested_actions
    assert enriched.manual_intervention_needed == report.manual_intervention_needed
    assert enriched.message == report.message
    assert enriched.retry == report.retry


# --------------------------------------------------------------------------- #
# Patch retry eligibility (#26D1) — pure decision helper, no execution.
# --------------------------------------------------------------------------- #


def _eligible_report(
    failure_type: PatchFailureType = PatchFailureType.PATCH_DOES_NOT_APPLY,
    *,
    failure_report_id: str = "frid-1",
    attempts: list[PatchRecoveryAttempt] | None = None,
) -> PatchFailureReport:
    """A report whose surrounding inputs (below) make every check pass except
    the one a given test deliberately breaks."""
    report = build_patch_failure_report(failure_type, max_attempts=2)
    return report.model_copy(
        update={
            "failure_report_id": failure_report_id,
            "attempts": attempts or [],
        }
    )


def _evaluate(report, **overrides):
    """Evaluate with all-valid defaults; override one field per test."""
    kwargs = {
        "requested_failure_report_id": "frid-1",
        "dependencies_met": True,
        "working_tree_clean": True,
        "chunk_status": "failed",
    }
    kwargs.update(overrides)
    return evaluate_patch_retry_eligibility(report, **kwargs)


def _human_attempt(n: int, recovery_mode: str = "human") -> PatchRecoveryAttempt:
    return PatchRecoveryAttempt(
        attempt_id=f"a{n}",
        attempt_number=n,
        started_at="t",
        recovery_mode=recovery_mode,
    )


def test_eligibility_happy_path():
    decision = _evaluate(_eligible_report())
    assert isinstance(decision, PatchRetryEligibilityDecision)
    assert decision.eligible is True
    assert decision.reason is None
    assert decision.status_code is None
    assert decision.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY
    assert decision.human_retry_attempts_used == 0
    assert decision.max_human_retries == MAX_HUMAN_RETRIES


def test_eligibility_report_none_is_422():
    decision = evaluate_patch_retry_eligibility(
        None,
        requested_failure_report_id="frid-1",
        dependencies_met=True,
        working_tree_clean=True,
        chunk_status="failed",
    )
    assert decision.eligible is False
    assert decision.status_code == 422
    assert decision.reason == RETRY_INELIGIBLE_MISSING_REPORT
    assert decision.failure_type is None
    assert decision.human_retry_attempts_used == 0


def test_eligibility_missing_failure_report_id_is_409():
    report = _eligible_report(failure_report_id="")
    decision = _evaluate(report, requested_failure_report_id="")
    assert decision.eligible is False
    assert decision.status_code == 409
    assert decision.reason == RETRY_INELIGIBLE_MISSING_FAILURE_REPORT_ID


def test_eligibility_stale_failure_report_id_is_409():
    decision = _evaluate(
        _eligible_report(), requested_failure_report_id="frid-OTHER"
    )
    assert decision.eligible is False
    assert decision.status_code == 409
    assert decision.reason == RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID


def test_eligibility_chunk_not_failed_is_422():
    decision = _evaluate(_eligible_report(), chunk_status="approved")
    assert decision.eligible is False
    assert decision.status_code == 422
    assert decision.reason == RETRY_INELIGIBLE_CHUNK_NOT_FAILED


def test_eligibility_dependencies_not_met_is_422():
    decision = _evaluate(_eligible_report(), dependencies_met=False)
    assert decision.eligible is False
    assert decision.status_code == 422
    assert decision.reason == RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET


def test_eligibility_dirty_worktree_is_409():
    decision = _evaluate(_eligible_report(), working_tree_clean=False)
    assert decision.eligible is False
    assert decision.status_code == 409
    assert decision.reason == RETRY_INELIGIBLE_DIRTY_WORKTREE


@pytest.mark.parametrize(
    "failure_type",
    [
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        PatchFailureType.TARGET_MISSING,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED,
        PatchFailureType.HARNESS_ERROR,
    ],
)
def test_eligibility_allowed_failure_types(failure_type):
    decision = _evaluate(_eligible_report(failure_type))
    assert decision.eligible is True
    assert decision.failure_type == failure_type


@pytest.mark.parametrize(
    "failure_type",
    [
        PatchFailureType.SCOPE_VIOLATION,
        PatchFailureType.FORBIDDEN_FILE,
        PatchFailureType.PATCH_MALFORMED,
        PatchFailureType.NO_CHANGES,
        PatchFailureType.DIRTY_WORKTREE,
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        PatchFailureType.TEST_REGRESSION,
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED,
    ],
)
def test_eligibility_disallowed_failure_types_are_422(failure_type):
    decision = _evaluate(_eligible_report(failure_type))
    assert decision.eligible is False
    assert decision.status_code == 422
    assert decision.reason == RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE


def test_harness_error_is_human_retryable_where_old_test_failure_was_not():
    harness = _eligible_report(PatchFailureType.HARNESS_ERROR)
    old = _eligible_report(PatchFailureType.TEST_FAILURE_AFTER_APPLY)

    harness_decision = _evaluate(harness)
    old_decision = _evaluate(old)

    assert harness_decision.eligible is True
    assert old_decision.eligible is False
    assert old_decision.reason == RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE


def test_eligibility_cap_exhausted_is_422():
    # Allowed type + two human attempts => cap reached, type check passes first.
    report = _eligible_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        attempts=[_human_attempt(1), _human_attempt(2)],
    )
    decision = _evaluate(report)
    assert decision.eligible is False
    assert decision.status_code == 422
    assert decision.reason == RETRY_INELIGIBLE_CAP_EXHAUSTED
    assert decision.human_retry_attempts_used == 2


def test_eligibility_one_human_attempt_still_eligible():
    report = _eligible_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY, attempts=[_human_attempt(1)]
    )
    decision = _evaluate(report)
    assert decision.eligible is True
    assert decision.human_retry_attempts_used == 1


def test_eligibility_custom_max_human_retries():
    report = _eligible_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY, attempts=[_human_attempt(1)]
    )
    decision = _evaluate(report, max_human_retries=1)
    assert decision.eligible is False
    assert decision.reason == RETRY_INELIGIBLE_CAP_EXHAUSTED
    assert decision.max_human_retries == 1


# --------------------------------------------------------------------------- #
# count_human_retry_attempts (#26D1)
# --------------------------------------------------------------------------- #


def test_count_human_retry_attempts_counts_only_human_modes():
    report = _eligible_report(
        attempts=[
            _human_attempt(1, "initial"),
            _human_attempt(2, "auto"),
            _human_attempt(3, "human"),
            _human_attempt(4, "human_with_instruction"),
        ]
    )
    # initial + auto excluded; human + human_with_instruction counted.
    assert count_human_retry_attempts(report) == 2


def test_count_human_retry_attempts_zero_for_no_attempts():
    assert count_human_retry_attempts(_eligible_report()) == 0


def test_count_human_retry_attempts_initial_does_not_count():
    report = _eligible_report(attempts=[_human_attempt(1, "initial")])
    assert count_human_retry_attempts(report) == 0


def test_count_human_retry_attempts_auto_does_not_count():
    report = _eligible_report(
        attempts=[_human_attempt(1, "auto"), _human_attempt(2, "auto")]
    )
    assert count_human_retry_attempts(report) == 0


# --------------------------------------------------------------------------- #
# record_retry_attempt (#26D1)
# --------------------------------------------------------------------------- #


def test_record_retry_attempt_appends_next_number_and_updates_id():
    report = record_initial_attempt(
        build_patch_failure_report(
            PatchFailureType.PATCH_DOES_NOT_APPLY, max_attempts=2
        ),
        failure_report_id="frid-1",
        attempt_id="att-initial",
        started_at="t0",
    )
    assert report.failure_report_id == "frid-1"
    assert len(report.attempts) == 1

    retried = record_retry_attempt(
        report,
        failure_report_id="frid-2",
        attempt_id="att-retry",
        started_at="t1",
        failure_type=PatchFailureType.PATCH_DOES_NOT_APPLY,
        failed_step="patch",
        changed_files_attempted=["src/a.py"],
        human_decision="retry",
    )

    assert retried.failure_report_id == "frid-2"
    assert len(retried.attempts) == 2
    new = retried.attempts[-1]
    assert new.attempt_id == "att-retry"
    assert new.attempt_number == 2
    assert new.recovery_mode == "human"
    assert new.human_decision == "retry"
    assert new.changed_files_attempted == ["src/a.py"]

    # Pure: original report untouched.
    assert report.failure_report_id == "frid-1"
    assert len(report.attempts) == 1


def test_record_retry_attempt_default_recovery_mode_is_human():
    report = _eligible_report()
    retried = record_retry_attempt(
        report, failure_report_id="frid-2", attempt_id="a", started_at="t"
    )
    assert retried.attempts[-1].recovery_mode == "human"


def test_record_retry_attempt_caps_attempts():
    base = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY, max_attempts=2
    )
    prefilled = [
        PatchRecoveryAttempt(
            attempt_id=f"a{i}", attempt_number=i + 1, started_at="t"
        )
        for i in range(MAX_RECOVERY_ATTEMPTS)
    ]
    report = base.model_copy(update={"attempts": prefilled})

    retried = record_retry_attempt(
        report, failure_report_id="frid-2", attempt_id="new", started_at="t"
    )
    assert len(retried.attempts) == MAX_RECOVERY_ATTEMPTS
    assert retried.attempts[-1].attempt_id == "new"
    assert all(a.attempt_id != "a0" for a in retried.attempts)


def test_record_retry_attempt_preserves_old_report_fields():
    report = _eligible_report(PatchFailureType.TARGET_MISSING)
    retried = record_retry_attempt(
        report, failure_report_id="frid-2", attempt_id="a", started_at="t"
    )
    assert retried.failure_type == report.failure_type
    assert retried.message == report.message
    assert retried.suggested_actions == report.suggested_actions
    assert retried.manual_intervention_needed == report.manual_intervention_needed
    assert retried.retry == report.retry


def test_record_retry_attempt_has_no_sensitive_keys():
    import json

    report = _eligible_report()
    retried = record_retry_attempt(
        report,
        failure_report_id="frid-2",
        attempt_id="a",
        started_at="t",
        changed_files_attempted=["src/a.py"],
    )
    data = patch_failure_report_to_completion_summary(retried)
    attempts_blob = json.dumps(data["attempts"])
    for forbidden in ("old_string", "new_string", "content", "token", "secret"):
        assert forbidden not in attempts_blob


# --------------------------------------------------------------------------- #
# RecoveredPatchReviewSummary (#26D1)
# --------------------------------------------------------------------------- #


def test_recovered_patch_review_summary_serializes_with_kind():
    summary = RecoveredPatchReviewSummary(
        failure_report_id="frid-1",
        recovery_attempt_id="att-2",
        attempts=[_human_attempt(1, "human")],
        weak_test_warning=True,
    )
    data = recovered_patch_review_to_completion_summary(summary)
    assert data["kind"] == RECOVERED_PATCH_REVIEW_KIND
    assert data["kind"] == "recovered_patch_review"
    assert data["failure_report_id"] == "frid-1"
    assert data["recovery_attempt_id"] == "att-2"
    assert data["weak_test_warning"] is True
    assert isinstance(data["attempts"], list)


def test_recovered_patch_review_does_not_look_like_patch_failure():
    summary = RecoveredPatchReviewSummary(
        failure_report_id="frid-1", recovery_attempt_id="att-2"
    )
    data = recovered_patch_review_to_completion_summary(summary)
    assert data["kind"] != PATCH_FAILURE_KIND
    # The patch-failure parser must ignore the recovered-review shape.
    assert patch_failure_report_from_completion_summary(data) is None


def test_recovered_patch_review_defaults():
    summary = RecoveredPatchReviewSummary(
        failure_report_id="frid-1", recovery_attempt_id="att-2"
    )
    assert summary.kind == "recovered_patch_review"
    assert summary.attempts == []
    assert summary.weak_test_warning is None


# ==========================================================================
# human_retry_ineligible_reason (#40C)
# ==========================================================================


ALL_RETRY_INELIGIBLE_REASONS = [
    RETRY_INELIGIBLE_MISSING_REPORT,
    RETRY_INELIGIBLE_MISSING_FAILURE_REPORT_ID,
    RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID,
    RETRY_INELIGIBLE_CHUNK_NOT_FAILED,
    RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET,
    RETRY_INELIGIBLE_DIRTY_WORKTREE,
    RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE,
    RETRY_INELIGIBLE_CAP_EXHAUSTED,
    RETRY_INELIGIBLE_WRONG_BRANCH,
]


@pytest.mark.parametrize("reason", ALL_RETRY_INELIGIBLE_REASONS)
def test_human_retry_ineligible_reason_is_prose_for_every_identifier(reason):
    message = human_retry_ineligible_reason(reason)
    # Plain prose: the raw snake_case identifier never appears, and the result
    # reads like a sentence (has spaces, no underscores, ends with a period).
    assert reason not in message
    assert "_" not in message
    assert " " in message
    assert message.endswith(".")


def test_human_retry_ineligible_reason_specific_copy():
    assert human_retry_ineligible_reason(RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE) == (
        "This kind of failure cannot be retried automatically."
    )
    assert human_retry_ineligible_reason(RETRY_INELIGIBLE_CAP_EXHAUSTED) == (
        "The retry limit for this failure has already been reached."
    )
    assert human_retry_ineligible_reason(RETRY_INELIGIBLE_CHUNK_NOT_FAILED) == (
        "Retry is only available while the chunk is in a failed state."
    )


def test_human_retry_ineligible_reason_none_and_unknown_fall_back_safely():
    fallback = "This change cannot be retried right now."
    assert human_retry_ineligible_reason(None) == fallback
    assert human_retry_ineligible_reason("") == fallback
    assert human_retry_ineligible_reason("some_unknown_reason") == fallback
