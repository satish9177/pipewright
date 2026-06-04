"""
pr_checks.py
Read-only GitHub PR checks/status foundation (#31D).

Display-only. This module NEVER gates final approval, push, or PR creation,
never merges, never comments, and never stores raw check logs — it keeps only
small aggregate counts and a derived state. The aggregation is pure; the single
network touch (`fetch_checks_summary`) is read-only and is invoked ONLY by an
explicit caller. It is deliberately NOT wired into the normal Run Detail / chunk
read path, so a routine page load never calls GitHub.

Honesty rule: a GitHub/gh failure is reported as ``unavailable``, never as
``failed``. A transient CLI/network problem must not look like a red build.

Note on gh exit codes: ``gh pr checks`` uses its exit code to signal check
*results* (non-zero when checks are failing or pending). That is handled in the
gh helper (gh_pr.get_pr_checks), which treats the JSON on stdout as
authoritative and only raises on a real CLI failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA_VERSION = 1


class ChecksState:
    """Derived, read-only summary state for a PR's checks. Display-only."""

    # No data yet / not determined.
    UNKNOWN = "unknown"
    # At least one check is still queued or running (and none have failed).
    PENDING = "pending"
    # Every concluded check succeeded (or was skipped); none failed/pending.
    PASSED = "passed"
    # At least one check concluded in failure (or was cancelled).
    FAILED = "failed"
    # Checks could not be retrieved (gh/network error). NOT a red build.
    UNAVAILABLE = "unavailable"
    # The PR exists but has no checks configured.
    NO_CHECKS = "no_checks"


# gh's normalized per-check ``bucket`` values, mapped to our count categories.
_BUCKET_PASS = "pass"
_BUCKET_FAIL = "fail"
_BUCKET_PENDING = "pending"
_BUCKET_SKIPPING = "skipping"
_BUCKET_CANCEL = "cancel"


@dataclass(frozen=True)
class ChecksSummary:
    """
    Compact, display-only summary of a PR's checks.

    Carries only aggregate counts and a derived state — never raw logs, never
    per-check output. Additive and read-only; it gates nothing.
    """

    state: str
    total: int
    passed: int
    failed: int
    pending: int
    skipped: int
    schema_version: int = SCHEMA_VERSION
    checked_at: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pending": self.pending,
            "skipped": self.skipped,
            "checked_at": self.checked_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_checks_state(
    *, total: int, failed: int, pending: int
) -> str:
    """
    Pure precedence: failed > pending > passed, with no checks => NO_CHECKS.

    Failure wins so a red check is never hidden behind a pending one; pending
    wins over passed so an in-progress run never reads as fully green.
    """
    if total <= 0:
        return ChecksState.NO_CHECKS
    if failed > 0:
        return ChecksState.FAILED
    if pending > 0:
        return ChecksState.PENDING
    return ChecksState.PASSED


def summarize_checks(
    raw_items: list[dict] | None,
    *,
    checked_at: str | None = None,
) -> ChecksSummary:
    """
    Aggregate raw `gh pr checks --json` rows into a compact summary.

    Pure: no I/O. Each row's gh ``bucket`` decides its category. ``cancel`` is
    counted as failed (a cancelled check did not pass); ``skipping`` is skipped;
    any unrecognized bucket is counted as skipped so an unmapped value can
    neither force a false red/green nor stick the summary in pending.
    """
    passed = failed = pending = skipped = 0
    for item in raw_items or []:
        bucket = str((item or {}).get("bucket") or "").strip().lower()
        if bucket == _BUCKET_PASS:
            passed += 1
        elif bucket in (_BUCKET_FAIL, _BUCKET_CANCEL):
            failed += 1
        elif bucket == _BUCKET_PENDING:
            pending += 1
        elif bucket == _BUCKET_SKIPPING:
            skipped += 1
        else:
            skipped += 1

    total = passed + failed + pending + skipped
    state = _derive_checks_state(total=total, failed=failed, pending=pending)
    return ChecksSummary(
        state=state,
        total=total,
        passed=passed,
        failed=failed,
        pending=pending,
        skipped=skipped,
        checked_at=checked_at,
    )


def unavailable_summary(*, checked_at: str | None = None) -> ChecksSummary:
    """A summary representing 'checks could not be retrieved'. Never 'failed'."""
    return ChecksSummary(
        state=ChecksState.UNAVAILABLE,
        total=0,
        passed=0,
        failed=0,
        pending=0,
        skipped=0,
        checked_at=checked_at,
    )


def _default_fetcher(repo_path: str, identifier: str) -> list[dict]:
    # Imported lazily so the pure aggregation above stays importable/testable
    # without the gh subprocess layer.
    from backend.git import gh_pr

    return gh_pr.get_pr_checks(repo_path, identifier)


def fetch_checks_summary(
    repo_path: str,
    identifier: str | int,
    *,
    fetcher: Callable[[str, str], list[dict]] | None = None,
    checked_at: str | None = None,
) -> ChecksSummary:
    """
    Read-only fetch + summarize for a PR's checks. Explicit-call only.

    Any failure to retrieve checks (gh missing, network error, bad output)
    becomes an ``unavailable`` summary — never ``failed`` — so a CLI/network
    problem can never masquerade as a failing build. The ``fetcher`` is
    injectable for tests; the default shells out to ``gh pr checks`` via
    gh_pr.get_pr_checks.
    """
    stamp = checked_at or _utc_now()
    fetch = fetcher or _default_fetcher
    try:
        raw = fetch(repo_path, str(identifier))
    except Exception:
        return unavailable_summary(checked_at=stamp)
    if raw is None:
        return unavailable_summary(checked_at=stamp)
    return summarize_checks(raw, checked_at=stamp)
