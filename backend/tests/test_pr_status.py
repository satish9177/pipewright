"""
Tests for the pure, read-only PR/push status read model (#31B).

No DB, git, GitHub, network, route, or frontend is exercised. These cover the
honest derivation of pr_state, the push-failure taxonomy, the assembled
PrStatus overlay, and the operator_state fix that makes push_failed read as a
failure rather than "ready to create a PR".
"""

import pytest

from backend.core.statuses import RunStatus
from backend.pipeline.operator_state import (
    OperatorStateContext,
    OperatorSafetyCheckStatus,
    compute_operator_state,
)
from backend.pipeline.pr_status import (
    PrState,
    PushFailureKind,
    build_pr_status,
    classify_push_failure,
    derive_pr_state,
)
from backend.projects.pr_modes import (
    PR_MODE_GITHUB_CLI,
    PR_MODE_LOCAL_ONLY,
    PR_MODE_MANUAL_TOKEN,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# derive_pr_state                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", [PR_MODE_GITHUB_CLI, PR_MODE_MANUAL_TOKEN])
def test_final_approved_remote_is_ready_to_push(mode):
    state = derive_pr_state(
        run_status=RunStatus.FINAL_APPROVED, pr_mode=mode, pr_url=None
    )
    assert state == PrState.READY_TO_PUSH


def test_final_approved_local_only_is_local_ready():
    state = derive_pr_state(
        run_status=RunStatus.FINAL_APPROVED, pr_mode=PR_MODE_LOCAL_ONLY, pr_url=None
    )
    assert state == PrState.LOCAL_READY


def test_pushing_is_pushing():
    state = derive_pr_state(
        run_status=RunStatus.PUSHING, pr_mode=PR_MODE_GITHUB_CLI, pr_url=None
    )
    assert state == PrState.PUSHING


def test_push_failed_is_push_failed_not_ready():
    state = derive_pr_state(
        run_status=RunStatus.PUSH_FAILED, pr_mode=PR_MODE_GITHUB_CLI, pr_url=None
    )
    assert state == PrState.PUSH_FAILED


def test_local_only_complete_is_local_complete():
    state = derive_pr_state(
        run_status=RunStatus.COMPLETE, pr_mode=PR_MODE_LOCAL_ONLY, pr_url=None
    )
    assert state == PrState.LOCAL_COMPLETE


def test_complete_with_pr_url_is_pr_open():
    state = derive_pr_state(
        run_status=RunStatus.COMPLETE,
        pr_mode=PR_MODE_GITHUB_CLI,
        pr_url="https://github.com/o/r/pull/7",
    )
    assert state == PrState.PR_OPEN


def test_pr_url_wins_even_if_status_says_push_failed():
    # Defensive: a durable PR URL is the success marker and must win.
    state = derive_pr_state(
        run_status=RunStatus.PUSH_FAILED,
        pr_mode=PR_MODE_GITHUB_CLI,
        pr_url="https://github.com/o/r/pull/9",
    )
    assert state == PrState.PR_OPEN


def test_early_run_is_not_started():
    state = derive_pr_state(
        run_status=RunStatus.RUNNING_CHUNKS, pr_mode=PR_MODE_GITHUB_CLI, pr_url=None
    )
    assert state == PrState.NOT_STARTED


def test_remote_complete_without_pr_url_is_unknown():
    # Remote-mode complete normally has a pr_url; missing it -> fail closed.
    state = derive_pr_state(
        run_status=RunStatus.COMPLETE, pr_mode=PR_MODE_GITHUB_CLI, pr_url=None
    )
    assert state == PrState.UNKNOWN


def test_missing_pr_mode_defaults_to_local_only():
    state = derive_pr_state(
        run_status=RunStatus.FINAL_APPROVED, pr_mode=None, pr_url=None
    )
    assert state == PrState.LOCAL_READY


# --------------------------------------------------------------------------- #
# classify_push_failure                                                       #
# --------------------------------------------------------------------------- #


def test_classify_none_and_blank():
    assert classify_push_failure(None) is None
    assert classify_push_failure("   ") is None


@pytest.mark.parametrize(
    "error,expected_kind",
    [
        (
            "GitHub CLI is selected, but gh is not installed or not authenticated. "
            "Run `gh auth login`, then retry PR creation.",
            PushFailureKind.GH_NOT_READY,
        ),
        ("Base branch 'pipewright-staging' is not on 'origin'.", PushFailureKind.REMOTE_BASE_MISSING),
        ("Could not verify base branch 'x' on 'origin': boom", PushFailureKind.BASE_VERIFY_FAILED),
        ("branch_safety.py: forbidden base branch: main.", PushFailureKind.FORBIDDEN_BASE),
        ("[GIT] working tree is dirty: a.py, b.py.", PushFailureKind.DIRTY_TREE),
        ("pr_orchestrator.py: expected local branch missing: pipewright/abc", PushFailureKind.BRANCH_MISSING),
        ("pr_orchestrator.py: failed to checkout pipewright/abc: nope", PushFailureKind.CHECKOUT_FAILED),
        ("[GIT] current branch is empty", PushFailureKind.WRONG_BRANCH),
        ("pr_orchestrator.py: origin remote does not match configured GitHub repo o/r", PushFailureKind.REMOTE_MISMATCH),
        ("Branch has no commits ahead of base; cannot push or create PR.", PushFailureKind.NO_COMMITS_AHEAD),
        ("[GIT] git rev-list failed: bad revision", PushFailureKind.LOCAL_BASE_REF_ERROR),
        ("gh_pr.py: PR was created but its URL/number could not be parsed from gh output.", PushFailureKind.PR_PARSE_UNCONFIRMED),
        ("pr_orchestrator.py: GitHub auth/repo failed: Bad credentials", PushFailureKind.AUTH_FAILED),
        ("[GIT] git push failed: Permission denied (publickey).", PushFailureKind.PERMISSION_DENIED),
        ("[GIT] git push failed: Could not resolve host: github.com", PushFailureKind.NETWORK),
        ("[GIT] git push failed: rejected non-fast-forward", PushFailureKind.PUSH_REJECTED),
        ("gh_pr.py: gh pr create failed: something opaque", PushFailureKind.GH_CLI_ERROR),
        ("totally unrecognized message", PushFailureKind.UNKNOWN),
    ],
)
def test_classify_kinds(error, expected_kind):
    result = classify_push_failure(error)
    assert result is not None
    assert result.kind == expected_kind
    # Every classification carries an actionable next step.
    assert result.next_action.strip()
    assert result.summary.strip()


def test_classify_no_commits_ahead_is_not_retryable():
    result = classify_push_failure("Branch has no commits ahead of base.")
    assert result is not None
    assert result.retryable is False


def test_classify_forbidden_base_is_not_retryable():
    result = classify_push_failure("forbidden base branch: develop.")
    assert result is not None
    assert result.retryable is False


def test_classify_local_base_ref_missing_message():
    # The #31C message that distinguishes a local base-compare failure from a
    # zero-commits-ahead verdict must classify as the local base ref error.
    result = classify_push_failure(
        "pr_orchestrator.py: could not compare branch against base "
        "'pipewright-staging' locally; the local base ref may be missing "
        "([GIT] git rev-list failed: bad revision)."
    )
    assert result is not None
    assert result.kind == PushFailureKind.LOCAL_BASE_REF_ERROR
    assert result.retryable is True


def test_classify_local_base_ref_not_confused_with_no_commits_ahead():
    # "no commits ahead" stays its own (non-retryable) kind and is not
    # swallowed by the local-base-ref markers.
    result = classify_push_failure(
        "Branch has no commits ahead of base; cannot push or create PR."
    )
    assert result is not None
    assert result.kind == PushFailureKind.NO_COMMITS_AHEAD
    assert result.retryable is False


# --------------------------------------------------------------------------- #
# build_pr_status                                                             #
# --------------------------------------------------------------------------- #


def test_build_push_failed_attaches_failure_and_error():
    status = build_pr_status(
        run_status=RunStatus.PUSH_FAILED,
        pr_mode=PR_MODE_GITHUB_CLI,
        branch_name="pipewright/abc12345",
        push_error="[GIT] working tree is dirty: a.py.",
    ).model_dump()
    assert status["pr_state"] == PrState.PUSH_FAILED
    assert status["is_terminal"] is False
    assert status["push_error"] == "[GIT] working tree is dirty: a.py."
    assert status["failure"]["kind"] == PushFailureKind.DIRTY_TREE
    assert status["failure"]["next_action"].strip()


def test_build_pr_open_hides_stale_push_error():
    # A since-recovered run can still carry an old push_error column; once a PR
    # URL exists, the overlay must not resurface that as a current failure.
    status = build_pr_status(
        run_status=RunStatus.COMPLETE,
        pr_mode=PR_MODE_GITHUB_CLI,
        pr_url="https://github.com/o/r/pull/3",
        pr_number=3,
        push_error="[GIT] git push failed: earlier transient error",
    ).model_dump()
    assert status["pr_state"] == PrState.PR_OPEN
    assert status["is_terminal"] is True
    assert status["failure"] is None
    assert status["push_error"] is None
    assert status["pr_url"].endswith("/pull/3")


def test_build_local_complete_is_terminal_no_failure():
    status = build_pr_status(
        run_status=RunStatus.COMPLETE,
        pr_mode=PR_MODE_LOCAL_ONLY,
        branch_name="pipewright/abc12345",
    ).model_dump()
    assert status["pr_state"] == PrState.LOCAL_COMPLETE
    assert status["is_terminal"] is True
    assert status["failure"] is None
    assert status["pr_url"] is None


def test_build_ready_to_push_carries_branch():
    status = build_pr_status(
        run_status=RunStatus.FINAL_APPROVED,
        pr_mode=PR_MODE_MANUAL_TOKEN,
        branch_name="pipewright/abc12345",
    ).model_dump()
    assert status["pr_state"] == PrState.READY_TO_PUSH
    assert status["branch_name"] == "pipewright/abc12345"
    assert status["failure"] is None


def test_build_pushing_has_no_failure():
    status = build_pr_status(
        run_status=RunStatus.PUSHING, pr_mode=PR_MODE_GITHUB_CLI
    ).model_dump()
    assert status["pr_state"] == PrState.PUSHING
    assert status["failure"] is None


# --------------------------------------------------------------------------- #
# operator_state: push_failed must NOT read as "Create pull request"          #
# --------------------------------------------------------------------------- #


def _op(**kwargs):
    return compute_operator_state(OperatorStateContext(**kwargs))


def test_operator_state_push_failed_is_honest_not_ready():
    failure = classify_push_failure("[GIT] working tree is dirty: a.py.")
    state = _op(
        run_status=RunStatus.PUSH_FAILED,
        pr_mode=PR_MODE_GITHUB_CLI,
        pr_ready=True,  # eligibility would otherwise offer "Create pull request"
        push_failed=True,
        push_failure_summary=failure.summary,
        push_failure_next_action=failure.next_action,
        push_failure_retryable=failure.retryable,
    )
    assert state.title == "Pull request could not be created"
    # The retry path stays available (the existing /push-pr route is idempotent).
    assert state.primary_action is not None
    assert state.primary_action.id == "create_pr"
    # Final approval is shown as a settled fact; the PR check is the failure.
    pr_check = next(c for c in state.safety_checks if c.id == "pr")
    assert pr_check.status == OperatorSafetyCheckStatus.FAILED


def test_operator_state_push_failed_non_retryable_blocks_retry():
    failure = classify_push_failure("forbidden base branch: main.")
    state = _op(
        run_status=RunStatus.PUSH_FAILED,
        pr_mode=PR_MODE_GITHUB_CLI,
        pr_ready=True,
        push_failed=True,
        push_failure_summary=failure.summary,
        push_failure_next_action=failure.next_action,
        push_failure_retryable=failure.retryable,
    )
    assert state.title == "Pull request could not be created"
    assert state.primary_action is None
    assert any(a.id == "create_pr" for a in state.blocked_actions)


def test_operator_state_pr_created_still_wins_over_push_failed():
    # A recorded PR URL is success; even with a stale push_failed flag, the
    # panel should show the PR-created state, never the failure state.
    state = _op(
        pr_created=True,
        push_failed=True,
        pr_mode=PR_MODE_GITHUB_CLI,
    )
    assert state.title == "Pull request is ready"
