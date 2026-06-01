"""
patch_failures.py
Pure model/helper layer for patch failure recovery (PR #18B).

Implements the failure taxonomy and structured report defined in
docs/architecture/patch-failure-recovery.md (#18A). This module is
intentionally pure:

  - No filesystem, git, DB, index, network, or LLM access.
  - No runtime wiring into patch_applier or chunked_orchestrator.
  - Deterministic given its inputs.

It only defines the enum, the report/retry models, the stable action
vocabulary, and small deterministic helpers (default messages, suggested
actions, stale-index hint, a report factory, and completion_summary
serialization). Behavioral wiring lands in later PRs (#18C+).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Sequence

from pydantic import BaseModel, Field

from backend.llm.sanitize import sanitize_for_log


# Discriminator stored inside the existing chunks.completion_summary JSON so a
# failure report can be told apart from a normal success summary without any
# schema change (#18A: no schema change for now).
PATCH_FAILURE_KIND = "patch_failure"

# Conservative cap for stored/returned technical details. Keep this comfortably
# under the event-bus data budget (#18A: Event.data capped ~4000 bytes) and
# never echo large blobs or file contents.
MAX_TECHNICAL_DETAILS_CHARS = 4000

# Stable action identifiers surfaced to the frontend recovery UI (#18A §5).
ACTION_RETRY = "retry"
ACTION_RETRY_WITH_INSTRUCTION = "retry_with_instruction"
ACTION_REINDEX = "reindex"
ACTION_REJECT_CHUNK = "reject_chunk"
ACTION_MARK_MANUAL_INTERVENTION = "mark_manual_intervention"
ACTION_VIEW_DETAILS = "view_details"

# Deterministic priority order for rendering suggested actions.
_ACTION_ORDER = (
    ACTION_RETRY,
    ACTION_RETRY_WITH_INSTRUCTION,
    ACTION_REINDEX,
    ACTION_REJECT_CHUNK,
    ACTION_MARK_MANUAL_INTERVENTION,
    ACTION_VIEW_DETAILS,
)


class PatchFailureType(str, Enum):
    """Closed taxonomy of patch failures (#18A §1)."""

    PATCH_MALFORMED = "PATCH_MALFORMED"
    PATCH_DOES_NOT_APPLY = "PATCH_DOES_NOT_APPLY"
    PATCH_PARTIAL_APPLY_BLOCKED = "PATCH_PARTIAL_APPLY_BLOCKED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    FORBIDDEN_FILE = "FORBIDDEN_FILE"
    TARGET_MISSING = "TARGET_MISSING"
    STALE_INDEX_OR_FILE_CHANGED = "STALE_INDEX_OR_FILE_CHANGED"
    NO_CHANGES = "NO_CHANGES"
    TEST_FAILURE_AFTER_APPLY = "TEST_FAILURE_AFTER_APPLY"
    DIRTY_WORKTREE = "DIRTY_WORKTREE"
    UNKNOWN_PATCH_FAILURE = "UNKNOWN_PATCH_FAILURE"


# Transient categories that may be auto-retried while a retry budget remains.
# Deterministic failures (SCOPE_VIOLATION, FORBIDDEN_FILE) are deliberately
# excluded: re-running the same plan reproduces the same violation.
_RETRYABLE_TRANSIENT: frozenset[PatchFailureType] = frozenset(
    {
        PatchFailureType.PATCH_MALFORMED,
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED,
        PatchFailureType.TARGET_MISSING,
        PatchFailureType.TEST_FAILURE_AFTER_APPLY,
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
    }
)

# Categories where replanning with a human instruction is a safe recovery.
_RETRY_WITH_INSTRUCTION_TYPES: frozenset[PatchFailureType] = (
    _RETRYABLE_TRANSIENT
    | frozenset(
        {
            PatchFailureType.SCOPE_VIOLATION,
            PatchFailureType.NO_CHANGES,
        }
    )
)

# Categories where re-indexing the repo is a meaningful recovery step.
_REINDEX_TYPES: frozenset[PatchFailureType] = frozenset(
    {
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED,
        PatchFailureType.TARGET_MISSING,
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED,
    }
)

# Categories that should surface the "repo index may be stale" hint.
_STALE_INDEX_HINT_TYPES: frozenset[PatchFailureType] = frozenset(
    {
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED,
        PatchFailureType.TARGET_MISSING,
        PatchFailureType.STALE_INDEX_OR_FILE_CHANGED,
    }
)


_DEFAULT_MESSAGES: dict[PatchFailureType, str] = {
    PatchFailureType.PATCH_MALFORMED: (
        "The generated change was malformed and could not be read as a patch."
    ),
    PatchFailureType.PATCH_DOES_NOT_APPLY: (
        "The change could not be applied to the current files. It may be based "
        "on an out-of-date version of the repo."
    ),
    PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED: (
        "Only part of this change could be applied cleanly, so nothing was "
        "applied to keep the repo consistent."
    ),
    PatchFailureType.SCOPE_VIOLATION: (
        "The change tried to edit files outside the approved chunk scope and "
        "was rejected."
    ),
    PatchFailureType.FORBIDDEN_FILE: (
        "The change tried to modify a protected file and was rejected for "
        "safety."
    ),
    PatchFailureType.TARGET_MISSING: (
        "A file this change expected to edit no longer exists in the repo."
    ),
    PatchFailureType.STALE_INDEX_OR_FILE_CHANGED: (
        "The repo changed since this plan was built, so the change is based on "
        "stale information."
    ),
    PatchFailureType.NO_CHANGES: (
        "This change produced no actual edits — it may already be present in "
        "the repo. Nothing was committed."
    ),
    PatchFailureType.TEST_FAILURE_AFTER_APPLY: (
        "The change applied but the project's tests failed, so it was rolled "
        "back."
    ),
    PatchFailureType.DIRTY_WORKTREE: (
        "You have uncommitted changes. Commit or stash them before running "
        "this chunk so it can be safely rolled back."
    ),
    PatchFailureType.UNKNOWN_PATCH_FAILURE: (
        "An unexpected error stopped this change. It was rolled back; no "
        "changes were kept."
    ),
}


class PatchFailureRetryInfo(BaseModel):
    """Retry budget snapshot for a single patch failure (#18A §5)."""

    attempts: int = 0
    max_attempts: int = 0
    retryable: bool = False


class PatchFailureReport(BaseModel):
    """Structured, human-safe patch failure report (#18A §4)."""

    failure_type: PatchFailureType
    message: str
    technical_details: str | None = None
    changed_files_attempted: list[str] = Field(default_factory=list)
    changed_files_actual: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    rollback_performed: bool = False
    working_tree_clean: bool = False
    retry: PatchFailureRetryInfo
    stale_index_hint: bool = False
    chunk_number: int | None = None
    failed_step: str = "patch"
    manual_intervention_needed: bool = False


def default_message_for_failure_type(failure_type: PatchFailureType) -> str:
    """Return the canonical, human-safe headline for a failure type."""
    return _DEFAULT_MESSAGES.get(
        failure_type,
        _DEFAULT_MESSAGES[PatchFailureType.UNKNOWN_PATCH_FAILURE],
    )


def stale_index_hint_for(failure_type: PatchFailureType) -> bool:
    """True if the failure should surface the stale-repo-index hint."""
    return failure_type in _STALE_INDEX_HINT_TYPES


def _has_retry_budget(attempts: int, max_attempts: int | None) -> bool:
    """True only when an auto-retry is still permitted by the budget."""
    if max_attempts is None or max_attempts <= 0:
        return False
    return attempts < max_attempts


def suggested_actions_for(
    failure_type: PatchFailureType,
    *,
    attempts: int = 0,
    max_attempts: int | None = None,
) -> list[str]:
    """
    Deterministic, bounded recovery action set for a failure (#18A §5).

    Rules:
      - view_details, reject_chunk, mark_manual_intervention are always offered.
      - Transient categories offer `retry` only while the budget remains;
        DIRTY_WORKTREE offers `retry` always (user-initiated after cleaning the
        tree, never auto-retried and not gated by the patch retry budget).
      - retry_with_instruction is offered for transient categories plus
        SCOPE_VIOLATION and NO_CHANGES.
      - reindex is offered for apply/target/stale categories.
      - Deterministic failures (SCOPE_VIOLATION, FORBIDDEN_FILE) never offer
        plain `retry`.
    """
    actions: set[str] = set()

    if failure_type == PatchFailureType.DIRTY_WORKTREE:
        actions.add(ACTION_RETRY)
    elif (
        failure_type in _RETRYABLE_TRANSIENT
        and _has_retry_budget(attempts, max_attempts)
    ):
        actions.add(ACTION_RETRY)

    if failure_type in _RETRY_WITH_INSTRUCTION_TYPES:
        actions.add(ACTION_RETRY_WITH_INSTRUCTION)

    if failure_type in _REINDEX_TYPES:
        actions.add(ACTION_REINDEX)

    # Always available: reject the chunk, escalate to a human, or inspect.
    actions.add(ACTION_REJECT_CHUNK)
    actions.add(ACTION_MARK_MANUAL_INTERVENTION)
    actions.add(ACTION_VIEW_DETAILS)

    return [action for action in _ACTION_ORDER if action in actions]


def _sanitize_technical_details(value: str | None) -> str | None:
    """Redact secret-like values and truncate to a conservative cap."""
    if value is None:
        return None
    text = sanitize_for_log(str(value))
    if len(text) > MAX_TECHNICAL_DETAILS_CHARS:
        text = text[:MAX_TECHNICAL_DETAILS_CHARS] + "\n[truncated]"
    return text


def build_patch_failure_report(
    failure_type: PatchFailureType,
    *,
    technical_details: str | None = None,
    changed_files_attempted: Sequence[str] = (),
    changed_files_actual: Sequence[str] = (),
    allowed_files: Sequence[str] = (),
    rollback_performed: bool = False,
    working_tree_clean: bool = False,
    attempts: int = 0,
    max_attempts: int | None = None,
    chunk_number: int | None = None,
    failed_step: str = "patch",
) -> PatchFailureReport:
    """
    Assemble a fully-populated, human-safe PatchFailureReport.

    Pure: derives the message, suggested actions, retry info, stale-index hint,
    and manual-intervention flag deterministically from the inputs. Input
    sequences are copied so the report never aliases caller-owned lists.
    """
    actions = suggested_actions_for(
        failure_type,
        attempts=attempts,
        max_attempts=max_attempts,
    )
    retry_available = ACTION_RETRY in actions

    # A retry budget is "exhausted" only for genuinely retryable transient
    # categories that were given a positive budget and have used it up.
    cap_exhausted = (
        failure_type in _RETRYABLE_TRANSIENT
        and max_attempts is not None
        and max_attempts > 0
        and attempts >= max_attempts
    )

    # Escalate to a human when retries are spent, or when a rollback ran but
    # left the tree dirty (the dangerous case from #18A: rollback must restore
    # a clean tree, or we must not claim recovery).
    manual_intervention_needed = bool(cap_exhausted) or (
        rollback_performed and not working_tree_clean
    )

    return PatchFailureReport(
        failure_type=failure_type,
        message=default_message_for_failure_type(failure_type),
        technical_details=_sanitize_technical_details(technical_details),
        changed_files_attempted=list(changed_files_attempted),
        changed_files_actual=list(changed_files_actual),
        allowed_files=list(allowed_files),
        suggested_actions=actions,
        rollback_performed=rollback_performed,
        working_tree_clean=working_tree_clean,
        retry=PatchFailureRetryInfo(
            attempts=attempts,
            max_attempts=max_attempts or 0,
            retryable=retry_available,
        ),
        stale_index_hint=stale_index_hint_for(failure_type),
        chunk_number=chunk_number,
        failed_step=failed_step,
        manual_intervention_needed=manual_intervention_needed,
    )


def patch_failure_report_to_completion_summary(
    report: PatchFailureReport,
) -> dict[str, Any]:
    """
    Serialize a report to the dict stored in chunks.completion_summary.

    Tagged with `kind: patch_failure` so readers can discriminate it from a
    normal success summary. Enum values are emitted as plain strings.
    """
    data = report.model_dump(mode="json")
    return {"kind": PATCH_FAILURE_KIND, **data}


def patch_failure_report_from_completion_summary(
    value: Any,
) -> PatchFailureReport | None:
    """
    Parse a stored completion_summary back into a PatchFailureReport.

    Defensive by design: returns None (never raises) for anything that is not a
    well-formed patch-failure payload — None, non-dict, a dict with a different
    or missing `kind`, or malformed/typed-wrong fields from storage.
    """
    if not isinstance(value, dict):
        return None
    if value.get("kind") != PATCH_FAILURE_KIND:
        return None

    payload = {key: item for key, item in value.items() if key != "kind"}
    try:
        return PatchFailureReport.model_validate(payload)
    except Exception:
        return None
