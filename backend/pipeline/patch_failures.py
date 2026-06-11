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
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.llm.sanitize import sanitize_for_log
from backend.pipeline.policy import HUMAN_ATTEMPT_BUDGET


# Discriminator stored inside the existing chunks.completion_summary JSON so a
# failure report can be told apart from a normal success summary without any
# schema change (#18A: no schema change for now).
PATCH_FAILURE_KIND = "patch_failure"

# Conservative cap for stored/returned technical details. Keep this comfortably
# under the event-bus data budget (#18A: Event.data capped ~4000 bytes) and
# never echo large blobs or file contents.
MAX_TECHNICAL_DETAILS_CHARS = 4000

# Cap on stored recovery attempts per failure summary (#26C). Bounds the JSON
# stored in chunks.completion_summary; oldest attempts are dropped past the cap.
MAX_RECOVERY_ATTEMPTS = 10

# Maximum human-triggered attempts permitted per failure (#26D1, item 13).
# Plain human retries and steered attempts share this one cap. Auto attempts
# and the initial failed apply do not count against it. Single-sourced in
# policy.HUMAN_ATTEMPT_BUDGET; this name stays as the local alias so existing
# callers and stored-report semantics are unchanged.
MAX_HUMAN_RETRIES = HUMAN_ATTEMPT_BUDGET

# Discriminator for the future success-pause marker stored in
# chunks.completion_summary once a recovered patch awaits human review (#26D1).
# Distinct from PATCH_FAILURE_KIND so the existing failure parser ignores it.
RECOVERED_PATCH_REVIEW_KIND = "recovered_patch_review"

# Stable reason identifiers returned by evaluate_patch_retry_eligibility (#26D1).
# Kept stable so callers/UI can branch on them without string-matching prose.
RETRY_INELIGIBLE_MISSING_REPORT = "missing_or_malformed_report"
RETRY_INELIGIBLE_MISSING_FAILURE_REPORT_ID = "missing_failure_report_id"
RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID = "stale_failure_report_id"
RETRY_INELIGIBLE_CHUNK_NOT_FAILED = "chunk_not_failed"
RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET = "dependencies_not_met"
RETRY_INELIGIBLE_DIRTY_WORKTREE = "dirty_worktree"
RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE = "disallowed_failure_type"
RETRY_INELIGIBLE_CAP_EXHAUSTED = "human_retry_cap_exhausted"
# Branch guard reason (#26D3a). NOT produced by evaluate_patch_retry_eligibility:
# that pure helper stays branch-agnostic. This is returned by a separate read-only
# pre-check in the orchestrator when the target repo's HEAD is not on the run
# branch (verify-only; retry never checks out or switches branches).
RETRY_INELIGIBLE_WRONG_BRANCH = "wrong_branch"

# Plain-language copy for each stable retry-ineligible reason (#40C). The raw
# identifiers above stay machine-stable for branching/audit, but they must never
# be the primary user-facing explanation (docs/design/failure-state-ux-cleanup.md
# §5). This is the single backend mapping; UI/read-model copy resolves through it.
_RETRY_INELIGIBLE_HUMAN_MESSAGES: dict[str, str] = {
    RETRY_INELIGIBLE_MISSING_REPORT: (
        "There is no usable failure report to retry from."
    ),
    RETRY_INELIGIBLE_MISSING_FAILURE_REPORT_ID: (
        "The failure report is missing the information needed to retry."
    ),
    RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID: (
        "This failure report is out of date. Refresh the run and try again."
    ),
    RETRY_INELIGIBLE_CHUNK_NOT_FAILED: (
        "Retry is only available while the chunk is in a failed state."
    ),
    RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET: (
        "Earlier chunks must be resolved before this one can be retried."
    ),
    RETRY_INELIGIBLE_DIRTY_WORKTREE: (
        "Commit or stash your uncommitted changes before retrying."
    ),
    RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE: (
        "This kind of failure cannot be retried automatically."
    ),
    RETRY_INELIGIBLE_CAP_EXHAUSTED: (
        "The retry limit for this failure has already been reached."
    ),
    RETRY_INELIGIBLE_WRONG_BRANCH: (
        "Switch to the run's branch before retrying."
    ),
}

# Safe fallback so an unknown/new identifier never leaks as primary copy.
_RETRY_INELIGIBLE_DEFAULT_MESSAGE = "This change cannot be retried right now."


def human_retry_ineligible_reason(reason: str | None) -> str:
    """Map a stable retry-ineligible reason identifier to plain-language copy.

    Pure and total: returns the safe default for None or any unrecognised
    identifier, so a raw token (e.g. ``disallowed_failure_type``) is never shown
    as the primary user-facing explanation. The raw identifier remains available
    to callers for diagnostics/audit.
    """
    if not reason:
        return _RETRY_INELIGIBLE_DEFAULT_MESSAGE
    return _RETRY_INELIGIBLE_HUMAN_MESSAGES.get(
        reason, _RETRY_INELIGIBLE_DEFAULT_MESSAGE
    )


# Stable action identifiers surfaced to the frontend recovery UI (#18A section 5).
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
    """Closed taxonomy of patch failures (#18A section 1)."""

    PATCH_MALFORMED = "PATCH_MALFORMED"
    PATCH_DOES_NOT_APPLY = "PATCH_DOES_NOT_APPLY"
    PATCH_PARTIAL_APPLY_BLOCKED = "PATCH_PARTIAL_APPLY_BLOCKED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    FORBIDDEN_FILE = "FORBIDDEN_FILE"
    TARGET_MISSING = "TARGET_MISSING"
    STALE_INDEX_OR_FILE_CHANGED = "STALE_INDEX_OR_FILE_CHANGED"
    NO_CHANGES = "NO_CHANGES"
    TEST_FAILURE_AFTER_APPLY = "TEST_FAILURE_AFTER_APPLY"
    TEST_REGRESSION = "TEST_REGRESSION"
    HARNESS_ERROR = "HARNESS_ERROR"
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
        PatchFailureType.HARNESS_ERROR,
        PatchFailureType.UNKNOWN_PATCH_FAILURE,
    }
)

# Categories where regenerating with a human instruction (a steered attempt,
# item 13) is a safe recovery. TEST_REGRESSION joins its pre-split umbrella
# TEST_FAILURE_AFTER_APPLY here (proposal §4.2 "un-dead-ending TEST_REGRESSION
# (steerable)"): the item-8 split left the new type out of this set, so a
# regression was advertised as steerable only under the legacy type. Steerable
# is NOT plain-retryable: TEST_REGRESSION stays out of _RETRYABLE_TRANSIENT.
_RETRY_WITH_INSTRUCTION_TYPES: frozenset[PatchFailureType] = (
    _RETRYABLE_TRANSIENT
    | frozenset(
        {
            PatchFailureType.SCOPE_VIOLATION,
            PatchFailureType.NO_CHANGES,
            PatchFailureType.TEST_REGRESSION,
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

# Failure types where a human-triggered retry of the *same plan* is safe to even
# consider (#26D1). Deliberately a small allowlist — NOT _RETRYABLE_TRANSIENT,
# which also contains PATCH_MALFORMED / TEST_FAILURE_AFTER_APPLY /
# UNKNOWN_PATCH_FAILURE (all disallowed for human retry). HARNESS_ERROR is the
# one test-phase exception: after the bounded auto retry is spent, a human can
# retry once the environment is stable. Anything not in this set — including
# TEST_REGRESSION, SCOPE_VIOLATION, FORBIDDEN_FILE, NO_CHANGES, DIRTY_WORKTREE,
# STALE_INDEX_OR_FILE_CHANGED — is rejected. This module only *evaluates*
# eligibility; no retry is executed here (execution lands in #26D2+).
_HUMAN_RETRYABLE_FAILURE_TYPES: frozenset[PatchFailureType] = frozenset(
    {
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        PatchFailureType.TARGET_MISSING,
        PatchFailureType.PATCH_PARTIAL_APPLY_BLOCKED,
        PatchFailureType.HARNESS_ERROR,
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
    PatchFailureType.TEST_REGRESSION: (
        "The change applied and the test harness ran, but the project's tests "
        "failed, so the change was rolled back."
    ),
    PatchFailureType.HARNESS_ERROR: (
        "The change applied, but the test command did not produce a trustworthy "
        "test result, so the change was rolled back."
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
    """Retry budget snapshot for a single patch failure (#18A section 5)."""

    attempts: int = 0
    max_attempts: int = 0
    retryable: bool = False


class PatchRecoveryAttempt(BaseModel):
    """
    One patch-application attempt for a chunk (#26C — diagnostics foundation).

    Recorded purely for diagnostics and to seed future idempotency. No retry
    behavior exists yet: the only attempt recorded today is the initial failed
    apply (recovery_mode="initial"); retry-specific fields stay null until #26D.

    Never carries file contents, old_string/new_string, raw model output, or
    secrets — only paths already on the report, enums, flags, ids, and an ISO
    timestamp.
    """

    # `model_used` is a deliberate field name; opt out of Pydantic's protected
    # "model_" namespace so it does not warn (no behavior change).
    model_config = ConfigDict(protected_namespaces=())

    attempt_id: str
    attempt_number: int
    started_at: str
    recovery_mode: Literal[
        "initial", "human", "human_with_instruction", "auto"
    ] = "initial"
    failure_type: PatchFailureType | None = None
    failed_step: str | None = None
    changed_files_attempted: list[str] = Field(default_factory=list)
    changed_files_actual: list[str] = Field(default_factory=list)
    scope_ok: bool | None = None
    preimage_matched: bool | None = None
    model_used: str | None = None
    test_outcome: Literal["passed", "failed", "not_run"] = "not_run"
    outcome: Literal["failed", "recovered", "manual_intervention"] = "failed"
    human_decision: str | None = None
    working_tree_clean: bool = False
    rollback_performed: bool = False


class PatchFailureReport(BaseModel):
    """Structured, human-safe patch failure report (#18A section 4)."""

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
    # Recovery diagnostics (#26C). Optional/defaulted so pre-#26C summaries still
    # parse. attempts[] is append-only history capped at MAX_RECOVERY_ATTEMPTS.
    failure_report_id: str | None = None
    attempts: list[PatchRecoveryAttempt] = Field(default_factory=list)


class PatchRetryEligibilityDecision(BaseModel):
    """
    Pure decision about whether a human may retry a failed patch (#26D1).

    Produced by evaluate_patch_retry_eligibility from already-computed inputs.
    Carries no behavior: it does not retry, mutate state, or touch disk/git. The
    ``status_code`` is a *suggested* HTTP status for a future retry route (#26D3)
    and is None only when ``eligible`` is True.
    """

    eligible: bool
    reason: str | None = None
    status_code: int | None = None
    failure_type: PatchFailureType | None = None
    human_retry_attempts_used: int
    max_human_retries: int


class RecoveredPatchReviewSummary(BaseModel):
    """
    Marker stored in chunks.completion_summary when a retried patch applied
    successfully and is paused awaiting human review (#26D1).

    Pure data only. Tagged with its own ``kind`` so the existing patch-failure
    parser ignores it (kind != PATCH_FAILURE_KIND). Not wired into the
    orchestrator yet — execution/pause behavior lands in #26D2+.
    """

    kind: Literal["recovered_patch_review"] = RECOVERED_PATCH_REVIEW_KIND
    failure_report_id: str
    recovery_attempt_id: str
    attempts: list[PatchRecoveryAttempt] = Field(default_factory=list)
    weak_test_warning: bool | None = None


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
    Deterministic, bounded recovery action set for a failure (#18A section 5).

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


def record_initial_attempt(
    report: PatchFailureReport,
    *,
    failure_report_id: str,
    attempt_id: str,
    started_at: str,
) -> PatchFailureReport:
    """
    Return a copy of ``report`` enriched with a ``failure_report_id`` and an
    initial PatchRecoveryAttempt (#26C).

    Pure: ids and the timestamp are supplied by the caller so
    build_patch_failure_report stays deterministic. If the report already
    carries attempts, the new one is appended and the list is capped to the most
    recent MAX_RECOVERY_ATTEMPTS. Only data already present on the report is
    copied — never file contents, old_string/new_string, raw model output, or
    secrets. The new attempt always records the current (failed) state, so
    recovery_mode is "initial" and outcome is never "recovered" here.
    """
    existing = list(report.attempts)
    attempt_number = len(existing) + 1

    # scope_ok: false only when the failure is a scope drift; true otherwise.
    scope_ok = report.failure_type != PatchFailureType.SCOPE_VIOLATION

    test_outcome = (
        "failed"
        if report.failure_type
        in {
            PatchFailureType.TEST_FAILURE_AFTER_APPLY,
            PatchFailureType.TEST_REGRESSION,
            PatchFailureType.HARNESS_ERROR,
        }
        else "not_run"
    )
    outcome = (
        "manual_intervention"
        if report.manual_intervention_needed
        else "failed"
    )

    attempt = PatchRecoveryAttempt(
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        started_at=started_at,
        recovery_mode="initial",
        failure_type=report.failure_type,
        failed_step=report.failed_step,
        changed_files_attempted=list(report.changed_files_attempted),
        changed_files_actual=list(report.changed_files_actual),
        scope_ok=scope_ok,
        preimage_matched=None,
        model_used=None,
        test_outcome=test_outcome,
        outcome=outcome,
        human_decision=None,
        working_tree_clean=report.working_tree_clean,
        rollback_performed=report.rollback_performed,
    )

    capped = (existing + [attempt])[-MAX_RECOVERY_ATTEMPTS:]
    return report.model_copy(
        update={
            "failure_report_id": failure_report_id,
            "attempts": capped,
        }
    )


def count_human_retry_attempts(report: PatchFailureReport) -> int:
    """
    Count human-triggered retry attempts recorded on ``report`` (#26D1).

    Only ``recovery_mode`` of "human" or "human_with_instruction" counts toward
    the human retry cap. The initial failed apply ("initial") and any automatic
    retries ("auto") are deliberately excluded.
    """
    return sum(
        1
        for attempt in report.attempts
        if attempt.recovery_mode in ("human", "human_with_instruction")
    )


def evaluate_patch_retry_eligibility(
    report: PatchFailureReport | None,
    *,
    requested_failure_report_id: str | None,
    dependencies_met: bool,
    working_tree_clean: bool,
    chunk_status: str,
    max_human_retries: int = MAX_HUMAN_RETRIES,
) -> PatchRetryEligibilityDecision:
    """
    Decide whether a human may retry a failed patch (#26D1).

    Pure and deterministic: receives already-computed booleans and never reads
    disk, calls git, parses JSON, or mutates state. The caller is responsible for
    computing ``dependencies_met`` / ``working_tree_clean`` / ``chunk_status``
    and for honoring the suggested ``status_code`` at the route layer (#26D3).

    Ineligible results carry a stable ``reason`` and a suggested status:
      - missing/malformed report            -> 422
      - missing failure_report_id           -> 409
      - requested id != report id (stale)   -> 409
      - chunk not in "failed" status        -> 422
      - dependencies not met                -> 422
      - dirty worktree                      -> 409
      - disallowed failure type             -> 422
      - human retry cap exhausted           -> 422
    """
    return _evaluate_human_attempt_eligibility(
        report,
        requested_failure_report_id=requested_failure_report_id,
        dependencies_met=dependencies_met,
        working_tree_clean=working_tree_clean,
        chunk_status=chunk_status,
        max_human_retries=max_human_retries,
        allowed_failure_types=_HUMAN_RETRYABLE_FAILURE_TYPES,
    )


def evaluate_patch_steer_eligibility(
    report: PatchFailureReport | None,
    *,
    requested_failure_report_id: str | None,
    dependencies_met: bool,
    working_tree_clean: bool,
    chunk_status: str,
    max_human_retries: int = MAX_HUMAN_RETRIES,
) -> PatchRetryEligibilityDecision:
    """
    Decide whether a human may steer a failed patch (item 13).

    Identical gates, reasons, and status codes as
    evaluate_patch_retry_eligibility — the steer-less retry is the special case
    — except the allowed failure types are _RETRY_WITH_INSTRUCTION_TYPES (the
    set already advertised to the UI as ACTION_RETRY_WITH_INSTRUCTION). Steered
    and plain human attempts share one budget (count_human_retry_attempts
    counts recovery_mode "human" and "human_with_instruction" alike).
    """
    return _evaluate_human_attempt_eligibility(
        report,
        requested_failure_report_id=requested_failure_report_id,
        dependencies_met=dependencies_met,
        working_tree_clean=working_tree_clean,
        chunk_status=chunk_status,
        max_human_retries=max_human_retries,
        allowed_failure_types=_RETRY_WITH_INSTRUCTION_TYPES,
    )


def _evaluate_human_attempt_eligibility(
    report: PatchFailureReport | None,
    *,
    requested_failure_report_id: str | None,
    dependencies_met: bool,
    working_tree_clean: bool,
    chunk_status: str,
    max_human_retries: int,
    allowed_failure_types: frozenset[PatchFailureType],
) -> PatchRetryEligibilityDecision:
    """Shared gate sequence for human retry and steer eligibility."""
    # The None guard must come first: count_human_retry_attempts needs a report.
    if report is None:
        return PatchRetryEligibilityDecision(
            eligible=False,
            reason=RETRY_INELIGIBLE_MISSING_REPORT,
            status_code=422,
            failure_type=None,
            human_retry_attempts_used=0,
            max_human_retries=max_human_retries,
        )

    used = count_human_retry_attempts(report)

    def _ineligible(reason: str, status_code: int) -> PatchRetryEligibilityDecision:
        return PatchRetryEligibilityDecision(
            eligible=False,
            reason=reason,
            status_code=status_code,
            failure_type=report.failure_type,
            human_retry_attempts_used=used,
            max_human_retries=max_human_retries,
        )

    if not report.failure_report_id:
        return _ineligible(RETRY_INELIGIBLE_MISSING_FAILURE_REPORT_ID, 409)

    if requested_failure_report_id != report.failure_report_id:
        return _ineligible(RETRY_INELIGIBLE_STALE_FAILURE_REPORT_ID, 409)

    if chunk_status != "failed":
        return _ineligible(RETRY_INELIGIBLE_CHUNK_NOT_FAILED, 422)

    if not dependencies_met:
        return _ineligible(RETRY_INELIGIBLE_DEPENDENCIES_NOT_MET, 422)

    if not working_tree_clean:
        return _ineligible(RETRY_INELIGIBLE_DIRTY_WORKTREE, 409)

    if report.failure_type not in allowed_failure_types:
        return _ineligible(RETRY_INELIGIBLE_DISALLOWED_FAILURE_TYPE, 422)

    if used >= max_human_retries:
        return _ineligible(RETRY_INELIGIBLE_CAP_EXHAUSTED, 422)

    return PatchRetryEligibilityDecision(
        eligible=True,
        reason=None,
        status_code=None,
        failure_type=report.failure_type,
        human_retry_attempts_used=used,
        max_human_retries=max_human_retries,
    )


def record_retry_attempt(
    report: PatchFailureReport,
    *,
    failure_report_id: str,
    attempt_id: str,
    started_at: str,
    recovery_mode: Literal["human", "human_with_instruction", "auto"] = "human",
    failure_type: PatchFailureType | None = None,
    failed_step: str | None = None,
    changed_files_attempted: list[str] | None = None,
    changed_files_actual: list[str] | None = None,
    scope_ok: bool | None = None,
    preimage_matched: bool | None = None,
    model_used: str | None = None,
    test_outcome: Literal["passed", "failed", "not_run"] = "not_run",
    outcome: Literal["failed", "recovered", "manual_intervention"] = "failed",
    human_decision: str | None = None,
    working_tree_clean: bool = False,
    rollback_performed: bool = False,
) -> PatchFailureReport:
    """
    Return a copy of ``report`` with a retry PatchRecoveryAttempt appended and
    ``failure_report_id`` updated to the supplied new id (#26D1).

    Pure: ids, timestamp, and every attempt field are supplied by the caller, so
    this stays deterministic and does no I/O. The new attempt is numbered
    ``len(existing attempts) + 1`` and the list is capped to the most recent
    MAX_RECOVERY_ATTEMPTS. Like the initial-attempt helper, it stores only
    paths/enums/flags/ids/timestamp — never file contents, old_string/new_string,
    raw model output, or secrets.

    For future #26D2/#26D3 use only; not wired into execution yet.
    """
    existing = list(report.attempts)
    attempt_number = len(existing) + 1

    attempt = PatchRecoveryAttempt(
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        started_at=started_at,
        recovery_mode=recovery_mode,
        failure_type=failure_type,
        failed_step=failed_step,
        changed_files_attempted=list(changed_files_attempted or []),
        changed_files_actual=list(changed_files_actual or []),
        scope_ok=scope_ok,
        preimage_matched=preimage_matched,
        model_used=model_used,
        test_outcome=test_outcome,
        outcome=outcome,
        human_decision=human_decision,
        working_tree_clean=working_tree_clean,
        rollback_performed=rollback_performed,
    )

    capped = (existing + [attempt])[-MAX_RECOVERY_ATTEMPTS:]
    return report.model_copy(
        update={
            "failure_report_id": failure_report_id,
            "attempts": capped,
        }
    )


def recovered_patch_review_to_completion_summary(
    summary: RecoveredPatchReviewSummary,
) -> dict[str, Any]:
    """
    Serialize a RecoveredPatchReviewSummary to the dict stored in
    chunks.completion_summary (#26D1).

    The ``kind`` discriminator is part of the model, so the emitted dict is
    self-tagged as ``recovered_patch_review`` and is therefore ignored by
    patch_failure_report_from_completion_summary.
    """
    return summary.model_dump(mode="json")


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
