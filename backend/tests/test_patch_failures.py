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
    MAX_TECHNICAL_DETAILS_CHARS,
    PATCH_FAILURE_KIND,
    PatchFailureReport,
    PatchFailureType,
    build_patch_failure_report,
    default_message_for_failure_type,
    patch_failure_report_from_completion_summary,
    patch_failure_report_to_completion_summary,
    stale_index_hint_for,
    suggested_actions_for,
)

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
