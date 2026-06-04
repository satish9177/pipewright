"""
Unit tests for the pure Scope Expansion Recovery foundation (#27B).

Covers eligibility evaluation, approved-file validation, the high-risk
scope-expansion denylist (write-path safety), the effective-scope merge helper,
and request lifecycle helpers. No filesystem, git, DB, route, orchestrator, or
pipeline wiring is exercised — every helper here is pure.
"""

import pytest

from backend.pipeline.patch_failures import PatchFailureType
from backend.pipeline.scope_expansion import (
    MAX_SCOPE_AMENDMENTS,
    SCOPE_EXPANSION_APPROVED_EMPTY,
    SCOPE_EXPANSION_APPROVED_FORBIDDEN,
    SCOPE_EXPANSION_APPROVED_INVALID_PATH,
    SCOPE_EXPANSION_APPROVED_NOT_SUBSET,
    SCOPE_EXPANSION_INELIGIBLE_AMENDMENTS_EXHAUSTED,
    SCOPE_EXPANSION_INELIGIBLE_DIRTY_WORKTREE,
    SCOPE_EXPANSION_INELIGIBLE_MANUAL_INTERVENTION,
    SCOPE_EXPANSION_INELIGIBLE_MISSING_FAILURE_REPORT_ID,
    SCOPE_EXPANSION_INELIGIBLE_NO_REQUESTED_FILES,
    SCOPE_EXPANSION_INELIGIBLE_NOT_SCOPE_VIOLATION,
    SCOPE_EXPANSION_INELIGIBLE_STALE_FAILURE_REPORT_ID,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_CHUNK_NOT_FAILED,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_DIRTY_WORKTREE,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE,
    SCOPE_EXPANSION_APPROVE_INELIGIBLE_STALE_REPORT,
    ScopeExpansionStatus,
    ScopeExpansionValidationError,
    can_approve,
    can_redrive_retry,
    compute_effective_files_expected,
    evaluate_scope_expansion_approve_retry_eligibility,
    evaluate_scope_expansion_eligibility,
    filter_requestable_files,
    is_in_force,
    is_scope_expansion_forbidden,
    is_transition_allowed,
    validate_approved_files,
)

pytestmark = pytest.mark.unit


def _eligible_kwargs(**overrides):
    """Baseline kwargs for an eligible clean SCOPE_VIOLATION; override per test."""
    base = dict(
        failure_type=PatchFailureType.SCOPE_VIOLATION,
        working_tree_clean=True,
        manual_intervention_needed=False,
        amendments_used=0,
        requested_extra_files=["src/helper.py"],
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Eligibility (tests 1-6)
# ---------------------------------------------------------------------------


def test_eligible_clean_scope_violation():
    decision = evaluate_scope_expansion_eligibility(**_eligible_kwargs())
    assert decision.eligible is True
    assert decision.reason is None
    assert decision.status_code is None
    assert decision.failure_type == PatchFailureType.SCOPE_VIOLATION
    assert decision.requestable_files == ["src/helper.py"]


def test_non_scope_violation_is_ineligible():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(failure_type=PatchFailureType.PATCH_DOES_NOT_APPLY)
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_NOT_SCOPE_VIOLATION
    assert decision.status_code == 422


def test_dirty_worktree_is_ineligible():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(working_tree_clean=False)
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_DIRTY_WORKTREE
    assert decision.status_code == 409


def test_manual_intervention_needed_is_ineligible():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(manual_intervention_needed=True)
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_MANUAL_INTERVENTION
    assert decision.status_code == 409


def test_amendments_exhausted_is_ineligible():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(amendments_used=MAX_SCOPE_AMENDMENTS)
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_AMENDMENTS_EXHAUSTED
    assert decision.status_code == 422


def test_empty_requested_extra_files_is_ineligible():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(requested_extra_files=[])
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_NO_REQUESTED_FILES
    assert decision.status_code == 422


def test_requested_files_all_forbidden_is_ineligible():
    # Filtering drops every requested path, so there is nothing approvable.
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(requested_extra_files=[".env", ".git/config"])
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_NO_REQUESTED_FILES


def test_string_failure_type_is_accepted():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(failure_type="SCOPE_VIOLATION")
    )
    assert decision.eligible is True
    assert decision.failure_type == PatchFailureType.SCOPE_VIOLATION


def test_stale_failure_report_id_is_ineligible_when_token_supplied():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(
            report_failure_report_id="report-current",
            requested_failure_report_id="report-stale",
        )
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_STALE_FAILURE_REPORT_ID
    assert decision.status_code == 409


def test_missing_failure_report_id_is_ineligible_when_token_supplied():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(
            report_failure_report_id=None,
            requested_failure_report_id="report-1",
        )
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_INELIGIBLE_MISSING_FAILURE_REPORT_ID
    assert decision.status_code == 409


def test_matching_failure_report_id_is_eligible():
    decision = evaluate_scope_expansion_eligibility(
        **_eligible_kwargs(
            report_failure_report_id="report-1",
            requested_failure_report_id="report-1",
        )
    )
    assert decision.eligible is True


def _approve_retry_kwargs(**overrides):
    base = dict(
        chunk_plan_status="approved",
        request_status=ScopeExpansionStatus.PENDING,
        chunk_status="failed",
        has_patch_failure_report=True,
        report_failure_report_id="frid-1",
        request_failure_report_id="frid-1",
        working_tree_clean=True,
        failure_type=PatchFailureType.SCOPE_VIOLATION,
        manual_intervention_needed=False,
        amendments_used=0,
        requested_extra_files=["src/helper.py"],
    )
    base.update(overrides)
    return base


def test_approve_retry_pending_request_is_eligible():
    decision = evaluate_scope_expansion_approve_retry_eligibility(
        **_approve_retry_kwargs()
    )
    assert decision.eligible is True
    assert decision.is_pending is True
    assert decision.is_redrive is False


def test_approve_retry_approved_request_is_redrivable_without_cap_recheck():
    decision = evaluate_scope_expansion_approve_retry_eligibility(
        **_approve_retry_kwargs(
            request_status=ScopeExpansionStatus.APPROVED,
            amendments_used=MAX_SCOPE_AMENDMENTS,
            requested_extra_files=[],
        )
    )
    assert decision.eligible is True
    assert decision.is_pending is False
    assert decision.is_redrive is True


@pytest.mark.parametrize(
    ("request_status", "reason"),
    [
        (ScopeExpansionStatus.APPLIED, SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE),
        (ScopeExpansionStatus.REJECTED, SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE),
        (ScopeExpansionStatus.SUPERSEDED, SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE),
    ],
)
def test_approve_retry_terminal_request_statuses_are_ineligible(
    request_status,
    reason,
):
    decision = evaluate_scope_expansion_approve_retry_eligibility(
        **_approve_retry_kwargs(request_status=request_status)
    )
    assert decision.eligible is False
    assert decision.reason == reason
    assert decision.status_code == 409


def test_approve_retry_rejects_non_failed_chunk():
    decision = evaluate_scope_expansion_approve_retry_eligibility(
        **_approve_retry_kwargs(chunk_status="awaiting_chunk_approval")
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_CHUNK_NOT_FAILED


def test_approve_retry_rejects_stale_failure_report():
    decision = evaluate_scope_expansion_approve_retry_eligibility(
        **_approve_retry_kwargs(request_failure_report_id="frid-old")
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_STALE_REPORT


def test_approve_retry_rejects_dirty_worktree():
    decision = evaluate_scope_expansion_approve_retry_eligibility(
        **_approve_retry_kwargs(working_tree_clean=False)
    )
    assert decision.eligible is False
    assert decision.reason == SCOPE_EXPANSION_APPROVE_INELIGIBLE_DIRTY_WORKTREE


# ---------------------------------------------------------------------------
# Approved-file validation (tests 7-16)
# ---------------------------------------------------------------------------


def test_approved_subset_validation_succeeds():
    approved = validate_approved_files(
        requested_files=["src/a.py", "src/b.py", "src/c.py"],
        approved_files=["src/a.py", "src/c.py"],
    )
    assert approved == ["src/a.py", "src/c.py"]


def test_approved_subset_normalizes_and_dedupes():
    approved = validate_approved_files(
        requested_files=["src/a.py"],
        approved_files=["src\\a.py", "src/a.py"],
    )
    assert approved == ["src/a.py"]


def test_approved_file_outside_requested_fails():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(
            requested_files=["src/a.py"],
            approved_files=["src/other.py"],
        )
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_NOT_SUBSET


def test_empty_approved_set_is_invalid():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(requested_files=["src/a.py"], approved_files=[])
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_EMPTY


def test_absolute_path_fails():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(
            requested_files=["/etc/passwd"],
            approved_files=["/etc/passwd"],
        )
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_INVALID_PATH


def test_windows_drive_absolute_path_fails():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(
            requested_files=["C:\\secret.txt"],
            approved_files=["C:\\secret.txt"],
        )
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_INVALID_PATH


def test_traversal_path_fails():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(
            requested_files=["../outside.py"],
            approved_files=["../outside.py"],
        )
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_INVALID_PATH


def test_glob_directory_approval_fails():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(
            requested_files=["src/*.py"],
            approved_files=["src/*.py"],
        )
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_INVALID_PATH


def test_git_path_fails():
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(
            requested_files=[".git/config"],
            approved_files=[".git/config"],
        )
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_FORBIDDEN


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "migrations/0001_init.py",
        "migration/0001_init.py",
        "alembic/versions/abc123.py",
        "pyproject.toml",
        ".env",
        ".env.production",
        "config/secrets.json",
        "lib/private/key.pem",
        "requirements.txt",
        "package-lock.json",
        "Dockerfile",
        "docker-compose.yml",
    ],
)
def test_high_risk_paths_fail(path):
    assert is_scope_expansion_forbidden(path) is True
    with pytest.raises(ScopeExpansionValidationError) as excinfo:
        validate_approved_files(requested_files=[path], approved_files=[path])
    assert excinfo.value.reason == SCOPE_EXPANSION_APPROVED_FORBIDDEN


def test_ordinary_source_path_is_not_forbidden():
    assert is_scope_expansion_forbidden("src/helper.py") is False
    assert is_scope_expansion_forbidden("backend/pipeline/coder.py") is False


def test_filter_requestable_drops_unsafe_and_forbidden():
    requestable = filter_requestable_files(
        [
            "src/a.py",
            "../escape.py",
            "/abs.py",
            ".git/config",
            "pyproject.toml",
            "src/a.py",  # duplicate
            "src/b.py",
        ]
    )
    assert requestable == ["src/a.py", "src/b.py"]


# ---------------------------------------------------------------------------
# Effective-scope merge (tests 17-19)
# ---------------------------------------------------------------------------


def test_effective_scope_preserves_original_order_then_extras():
    effective = compute_effective_files_expected(
        original_files_expected=["src/a.py", "src/b.py"],
        approved_extra_files=["src/c.py", "src/d.py"],
    )
    assert effective == ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]


def test_effective_scope_dedupes_across_original_and_extras():
    effective = compute_effective_files_expected(
        original_files_expected=["src/a.py", "src/b.py"],
        approved_extra_files=["src/b.py", "src\\a.py", "src/c.py"],
    )
    # Originals come first; duplicates (incl. separator variants) are dropped.
    assert effective == ["src/a.py", "src/b.py", "src/c.py"]


def test_effective_scope_does_not_mutate_inputs():
    original = ["src/a.py"]
    extras = ["src/b.py"]
    compute_effective_files_expected(original, extras)
    assert original == ["src/a.py"]
    assert extras == ["src/b.py"]


def test_validate_approved_files_does_not_mutate_inputs():
    requested = ["src/a.py", "src/b.py"]
    approved = ["src/a.py"]
    validate_approved_files(requested, approved)
    assert requested == ["src/a.py", "src/b.py"]
    assert approved == ["src/a.py"]


# ---------------------------------------------------------------------------
# Lifecycle helpers (tests 20-22)
# ---------------------------------------------------------------------------


def test_lifecycle_pending_to_approved_to_applied():
    assert is_transition_allowed(
        ScopeExpansionStatus.PENDING, ScopeExpansionStatus.APPROVED
    )
    assert is_transition_allowed(
        ScopeExpansionStatus.APPROVED, ScopeExpansionStatus.APPLIED
    )


def test_lifecycle_pending_can_be_approved():
    assert can_approve(ScopeExpansionStatus.PENDING) is True


def test_approved_not_applied_is_redrivable_and_not_newly_approvable():
    # Crash-window idempotency: approved-but-not-applied may be re-driven, but it
    # must NOT be treated as a fresh approval.
    assert can_redrive_retry(ScopeExpansionStatus.APPROVED) is True
    assert can_approve(ScopeExpansionStatus.APPROVED) is False


def test_applied_is_not_redrivable():
    assert can_redrive_retry(ScopeExpansionStatus.APPLIED) is False


@pytest.mark.parametrize(
    "status",
    [
        ScopeExpansionStatus.APPROVED,
        ScopeExpansionStatus.APPLIED,
        ScopeExpansionStatus.REJECTED,
        ScopeExpansionStatus.SUPERSEDED,
    ],
)
def test_non_pending_cannot_be_newly_approved(status):
    assert can_approve(status) is False


@pytest.mark.parametrize(
    "status",
    [
        ScopeExpansionStatus.APPLIED,
        ScopeExpansionStatus.REJECTED,
        ScopeExpansionStatus.SUPERSEDED,
    ],
)
def test_terminal_states_have_no_outgoing_transitions(status):
    for target in ScopeExpansionStatus:
        assert is_transition_allowed(status, target) is False


def test_in_force_statuses_are_approved_and_applied():
    assert is_in_force(ScopeExpansionStatus.APPROVED) is True
    assert is_in_force(ScopeExpansionStatus.APPLIED) is True
    assert is_in_force(ScopeExpansionStatus.PENDING) is False
    assert is_in_force(ScopeExpansionStatus.REJECTED) is False
    assert is_in_force(ScopeExpansionStatus.SUPERSEDED) is False


def test_lifecycle_helpers_accept_string_status():
    assert can_approve("pending") is True
    assert can_redrive_retry("approved") is True
    assert is_in_force("applied") is True
    assert is_transition_allowed("pending", "approved") is True
