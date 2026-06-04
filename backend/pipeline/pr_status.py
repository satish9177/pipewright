"""
pr_status.py
Pure, read-only PR / push status derivation for the run read model (#31B).

This module is deliberately I/O-free: it performs NO database read/write, NO git
call, NO GitHub/network call, NO push, and NO PR creation. Routes load the run
row (and the project's pr_mode) with their existing helpers and call these pure
functions to attach an honest, typed PR status to the read response.

Design notes (docs/design/github-pr-robustness-and-checks.md):
  - pr_state is DERIVED on read from columns Pipewright already persists
    (status, pr_url, push_error, pushed_at). It is NOT persisted in this slice.
  - This module never gates a human approval, never merges, never creates PR
    comments, and never adds a route.
  - push_error strings are already sanitized at the point they were persisted
    (pr_orchestrator._mark_push_failed). The classifier only pattern-matches the
    sanitized text into a stable taxonomy; it adds no new secret-bearing data.
  - GitHub checks/status are intentionally OUT OF SCOPE here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.core.statuses import RunStatus
from backend.projects.pr_modes import (
    PR_MODE_LOCAL_ONLY,
    normalize_pr_mode,
)

SCHEMA_VERSION = 1


class PrState:
    """
    Derived, read-only PR lifecycle state. Orthogonal to RunStatus.

    NOT persisted in #31B; computed on every read from the run row + pr_mode.
    """

    # Run has not reached the push/PR stage yet (still planning/executing/etc).
    NOT_STARTED = "not_started"
    # Remote mode, final-approved, no PR yet: Pipewright can push + create a PR.
    READY_TO_PUSH = "ready_to_push"
    # local_only, final-approved (or push_failed never happens here): the
    # operator pushes / opens the PR by hand. No Pipewright push.
    LOCAL_READY = "local_ready"
    # local_only run finished locally. Terminal-OK, no PR by design.
    LOCAL_COMPLETE = "local_complete"
    # A push/PR attempt is in progress.
    PUSHING = "pushing"
    # A push/PR attempt failed. The branch may or may not already be pushed; the
    # existing /push-pr path is idempotent and can be retried.
    PUSH_FAILED = "push_failed"
    # A PR exists or was reused for this run. pr_url is the durable success
    # marker. (GitHub check status is a separate, deferred dimension.)
    PR_OPEN = "pr_open"
    # Could not be derived safely. Display fails closed; offers no action.
    UNKNOWN = "unknown"


# Terminal-for-display states: no further in-app PR action is expected.
_TERMINAL_PR_STATES = frozenset({PrState.LOCAL_COMPLETE, PrState.PR_OPEN})


class PushFailureKind:
    """Stable taxonomy keys for a persisted (sanitized) push_error string."""

    GH_NOT_READY = "gh_not_ready"
    REMOTE_BASE_MISSING = "remote_base_missing"
    BASE_VERIFY_FAILED = "base_verify_failed"
    FORBIDDEN_BASE = "forbidden_base"
    LOCAL_BASE_REF_ERROR = "local_base_ref_error"
    NO_COMMITS_AHEAD = "no_commits_ahead"
    DIRTY_TREE = "dirty_tree"
    BRANCH_MISSING = "branch_missing"
    CHECKOUT_FAILED = "checkout_failed"
    WRONG_BRANCH = "wrong_branch"
    REMOTE_MISMATCH = "remote_mismatch"
    PERMISSION_DENIED = "permission_denied"
    NETWORK = "network"
    AUTH_FAILED = "auth_failed"
    PUSH_REJECTED = "push_rejected"
    PR_PARSE_UNCONFIRMED = "pr_parse_unconfirmed"
    GH_CLI_ERROR = "gh_cli_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PushFailureClassification:
    """A pure classification of a sanitized push_error string."""

    kind: str
    summary: str
    next_action: str
    retryable: bool

    def model_dump(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "next_action": self.next_action,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class PrStatus:
    """
    Typed, read-only PR status overlay for the run/chunk read model.

    Additive and computed on read; existing clients can ignore it. It never
    affects eligibility, approval, or any mutation.
    """

    pr_state: str
    pr_mode: str
    is_terminal: bool
    schema_version: int = SCHEMA_VERSION
    branch_name: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pushed_at: str | None = None
    pr_created_at: str | None = None
    push_error: str | None = None
    failure: PushFailureClassification | None = None
    # Display-only PR checks summary (#31D). Present only when a PR exists AND an
    # explicit caller supplied a freshly fetched summary; None otherwise. It is a
    # plain dict (ChecksSummary.model_dump()) so this module stays I/O-free and
    # does not depend on the checks fetcher.
    checks: dict[str, Any] | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pr_state": self.pr_state,
            "pr_mode": self.pr_mode,
            "is_terminal": self.is_terminal,
            "branch_name": self.branch_name,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "pushed_at": self.pushed_at,
            "pr_created_at": self.pr_created_at,
            "push_error": self.push_error,
            "failure": self.failure.model_dump() if self.failure else None,
            "checks": self.checks,
        }


def derive_pr_state(
    *,
    run_status: str | None,
    pr_mode: str | None,
    pr_url: str | None,
) -> str:
    """
    Derive the read-only PrState from the persisted run row + project pr_mode.

    Pure. Precedence is honest by construction:
      1. A recorded pr_url is the durable success marker -> PR_OPEN, regardless
         of status (matches the orchestrator's idempotent short-circuit).
      2. An in-flight push -> PUSHING.
      3. A recorded push failure -> PUSH_FAILED. This is the fix for "push_failed
         must not read as ready to create a PR."
      4. local_only never pushes: map final_approved -> LOCAL_READY and
         complete -> LOCAL_COMPLETE.
      5. Remote modes: final_approved -> READY_TO_PUSH.
      6. Anything earlier -> NOT_STARTED; unresolved edge -> UNKNOWN.
    """
    mode = normalize_pr_mode(pr_mode)

    if pr_url:
        return PrState.PR_OPEN

    if run_status == RunStatus.PUSHING:
        return PrState.PUSHING

    if run_status == RunStatus.PUSH_FAILED:
        return PrState.PUSH_FAILED

    if mode == PR_MODE_LOCAL_ONLY:
        if run_status == RunStatus.COMPLETE:
            return PrState.LOCAL_COMPLETE
        if run_status == RunStatus.FINAL_APPROVED:
            return PrState.LOCAL_READY
        return PrState.NOT_STARTED

    # Remote modes (github_cli / manual_token), no pr_url yet.
    if run_status == RunStatus.FINAL_APPROVED:
        return PrState.READY_TO_PUSH
    if run_status == RunStatus.COMPLETE:
        # A remote-mode complete run normally carries a pr_url; reaching here
        # means the success marker is missing. Fail closed rather than imply a
        # PR exists.
        return PrState.UNKNOWN
    return PrState.NOT_STARTED


# Ordered (marker substrings -> kind). First match wins. All lowercase; the
# classifier lowercases the error before matching. Specific markers come before
# generic ones.
_FAILURE_MARKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("not installed or not authenticated", "gh auth login"), PushFailureKind.GH_NOT_READY),
    (("forbidden base branch",), PushFailureKind.FORBIDDEN_BASE),
    (("is not on",), PushFailureKind.REMOTE_BASE_MISSING),
    (("could not verify base branch",), PushFailureKind.BASE_VERIFY_FAILED),
    (("working tree is dirty",), PushFailureKind.DIRTY_TREE),
    (("expected local branch missing",), PushFailureKind.BRANCH_MISSING),
    (("failed to checkout",), PushFailureKind.CHECKOUT_FAILED),
    (("current branch is empty",), PushFailureKind.WRONG_BRANCH),
    (("origin remote does not match",), PushFailureKind.REMOTE_MISMATCH),
    (("no commits ahead",), PushFailureKind.NO_COMMITS_AHEAD),
    (("could not compare branch against base", "local base ref may be missing", "rev-list"), PushFailureKind.LOCAL_BASE_REF_ERROR),
    (("could not be parsed",), PushFailureKind.PR_PARSE_UNCONFIRMED),
    (("bad credentials", "auth/repo failed", "auth failed", "401"), PushFailureKind.AUTH_FAILED),
)

# Generic connectivity/permission markers, checked when a push/gh error did not
# match a more specific marker above.
_PERMISSION_MARKERS = ("permission denied", "403", "forbidden", "access denied", "denied to")
_NETWORK_MARKERS = (
    "could not resolve host",
    "connection timed out",
    "timed out",
    "network is unreachable",
    "connection refused",
    "failed to connect",
)

_KIND_DETAIL: dict[str, tuple[str, str, bool]] = {
    # kind: (summary, next_action, retryable)
    PushFailureKind.GH_NOT_READY: (
        "The GitHub CLI is not installed or not authenticated.",
        "Run `gh auth login`, then retry the push.",
        True,
    ),
    PushFailureKind.REMOTE_BASE_MISSING: (
        "The base branch does not exist on the remote.",
        "Push the base branch (e.g. `git push -u origin <base>`), then retry.",
        True,
    ),
    PushFailureKind.BASE_VERIFY_FAILED: (
        "Pipewright could not verify the base branch on the remote.",
        "Check the remote/connectivity, then retry.",
        True,
    ),
    PushFailureKind.FORBIDDEN_BASE: (
        "The configured base branch is protected (main/master/develop).",
        "Set the project's base branch to a non-protected branch, then retry.",
        False,
    ),
    PushFailureKind.LOCAL_BASE_REF_ERROR: (
        "Pipewright could not compare the branch against the base locally; the local base ref may be missing.",
        "Fetch the base branch locally (e.g. `git fetch origin <base>`), then retry.",
        True,
    ),
    PushFailureKind.NO_COMMITS_AHEAD: (
        "The branch has no commits ahead of the base; there is nothing to open a PR for.",
        "Investigate the run; no PR can be created from an empty diff.",
        False,
    ),
    PushFailureKind.DIRTY_TREE: (
        "The working tree has uncommitted changes.",
        "Review the working tree and restore/clean it if safe, then retry.",
        True,
    ),
    PushFailureKind.BRANCH_MISSING: (
        "The expected run branch is missing locally.",
        "Investigate the repository state before retrying.",
        False,
    ),
    PushFailureKind.CHECKOUT_FAILED: (
        "Pipewright could not checkout the expected run branch.",
        "Resolve the local git state, then retry.",
        True,
    ),
    PushFailureKind.WRONG_BRANCH: (
        "The repository is on a detached or unverifiable HEAD.",
        "Checkout the expected run branch, then retry.",
        True,
    ),
    PushFailureKind.REMOTE_MISMATCH: (
        "The `origin` remote does not match the configured GitHub repository.",
        "Fix the origin remote or the project's GitHub owner/repo, then retry.",
        True,
    ),
    PushFailureKind.PERMISSION_DENIED: (
        "Permission was denied by GitHub or the remote.",
        "Confirm your access to the repository, then retry.",
        False,
    ),
    PushFailureKind.NETWORK: (
        "A network error occurred while contacting the remote.",
        "Check connectivity, then retry.",
        True,
    ),
    PushFailureKind.AUTH_FAILED: (
        "GitHub authentication failed.",
        "Re-authenticate (token or `gh auth login`), then retry.",
        True,
    ),
    PushFailureKind.PUSH_REJECTED: (
        "The push was rejected by the remote.",
        "Inspect the remote branch state, then retry.",
        True,
    ),
    PushFailureKind.PR_PARSE_UNCONFIRMED: (
        "The PR may have been created, but its URL/number could not be confirmed.",
        "Retry; Pipewright will reuse the existing PR if one was created.",
        True,
    ),
    PushFailureKind.GH_CLI_ERROR: (
        "A GitHub CLI command failed.",
        "Inspect the error detail, then retry.",
        True,
    ),
    PushFailureKind.UNKNOWN: (
        "The push or PR creation failed.",
        "Inspect the error detail, then retry.",
        True,
    ),
}


def classify_push_failure(push_error: str | None) -> PushFailureClassification | None:
    """
    Classify a persisted (already sanitized) push_error into a stable taxonomy.

    Pure and read-only. Returns None when there is no error text. The original
    sanitized push_error is preserved separately on the read model; this only
    adds a machine-actionable kind + next-action so the UI need not parse prose.
    """
    if not push_error or not str(push_error).strip():
        return None

    text = str(push_error).lower()

    kind = PushFailureKind.UNKNOWN
    for markers, candidate in _FAILURE_MARKERS:
        if any(marker in text for marker in markers):
            kind = candidate
            break

    # For git-push / gh failures that did not hit a specific marker, refine into
    # permission vs network vs generic-reject so the next action is useful.
    if kind == PushFailureKind.UNKNOWN:
        is_push = "git push failed" in text or "push" in text
        is_gh = "gh_pr.py" in text or "gh command failed" in text or "gh pr" in text
        if any(marker in text for marker in _PERMISSION_MARKERS):
            kind = PushFailureKind.PERMISSION_DENIED
        elif any(marker in text for marker in _NETWORK_MARKERS):
            kind = PushFailureKind.NETWORK
        elif is_push:
            kind = PushFailureKind.PUSH_REJECTED
        elif is_gh:
            kind = PushFailureKind.GH_CLI_ERROR

    summary, next_action, retryable = _KIND_DETAIL[kind]
    return PushFailureClassification(
        kind=kind,
        summary=summary,
        next_action=next_action,
        retryable=retryable,
    )


def build_pr_status(
    *,
    run_status: str | None,
    pr_mode: str | None,
    branch_name: str | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
    pushed_at: str | None = None,
    pr_created_at: str | None = None,
    push_error: str | None = None,
    checks: dict[str, Any] | None = None,
) -> PrStatus:
    """
    Assemble the typed, read-only PrStatus overlay from a run row + pr_mode.

    Pure. The failure classification is attached only when the derived state is
    PUSH_FAILED, so a stale push_error from a since-recovered run never surfaces
    a failure once a pr_url exists or a fresh push is in flight.

    ``checks`` (#31D) is a pre-fetched, display-only ChecksSummary dict. It is
    attached ONLY when a PR exists (pr_state == PR_OPEN); for any other state it
    is dropped, so a stale or mistakenly-passed summary can never imply checks on
    a run that has no PR. This function performs NO fetch itself — an explicit
    caller fetches and passes the summary in.
    """
    mode = normalize_pr_mode(pr_mode)
    pr_state = derive_pr_state(
        run_status=run_status,
        pr_mode=mode,
        pr_url=pr_url,
    )

    failure = (
        classify_push_failure(push_error)
        if pr_state == PrState.PUSH_FAILED
        else None
    )

    return PrStatus(
        pr_state=pr_state,
        pr_mode=mode,
        is_terminal=pr_state in _TERMINAL_PR_STATES,
        branch_name=branch_name,
        pr_url=pr_url,
        pr_number=pr_number,
        pushed_at=pushed_at,
        pr_created_at=pr_created_at,
        push_error=push_error if pr_state == PrState.PUSH_FAILED else None,
        failure=failure,
        checks=checks if pr_state == PrState.PR_OPEN else None,
    )
