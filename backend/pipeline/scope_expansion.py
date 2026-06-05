"""
scope_expansion.py
Pure model/helper layer for Scope Expansion Recovery (#27B).

Implements the *foundation* described in
docs/design/scope-expansion-recovery.md (#27A): pure data models, request
lifecycle helpers, eligibility evaluation, approved-file validation, the
high-risk scope-expansion denylist, and the effective-scope merge helper.

This module is intentionally pure, mirroring patch_failures.py:

  - No filesystem, git, DB, index, network, or LLM access.
  - No runtime wiring into chunk_store, the orchestrator, or any route.
  - Deterministic given its inputs.

It does NOT change live pipeline behavior, does NOT make SCOPE_VIOLATION
retryable through the #26 public retry front door, and does NOT weaken
scope_guard. Runtime wiring (routes, overlay into get_chunk_plan_status, retry
execution) lands in later #27 slices.
"""

from __future__ import annotations

from enum import Enum
from pathlib import PureWindowsPath
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.pipeline.patch_failures import PatchFailureType
from backend.utils.path_safety import (
    is_forbidden_write_path,
    normalize_relative_path,
)

# At most one human-approved scope amendment per chunk in v1 (#27A decision #6).
# A chunk that needs more files than one amendment covers falls back to manual
# intervention; it is never expanded twice automatically.
MAX_SCOPE_AMENDMENTS = 1

# Glob/wildcard metacharacters. Scope approval is per-file only: directory or
# glob approvals are rejected so an approved allowlist is always concrete (#27A
# section 22: "Do not allow directory/glob approvals").
_GLOB_METACHARACTERS = ("*", "?", "[", "]")


class ScopeExpansionStatus(str, Enum):
    """Lifecycle states for a scope expansion request (#27A section 6)."""

    PENDING = "pending"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# Allowed lifecycle transitions (#27A section 6 / section 14). Idempotent re-drive of an
# already-approved-but-not-applied request is handled by can_redrive_retry, not
# modeled as a self-transition here.
_ALLOWED_TRANSITIONS: dict[ScopeExpansionStatus, frozenset[ScopeExpansionStatus]] = {
    ScopeExpansionStatus.PENDING: frozenset(
        {
            ScopeExpansionStatus.APPROVED,
            ScopeExpansionStatus.REJECTED,
            ScopeExpansionStatus.SUPERSEDED,
        }
    ),
    ScopeExpansionStatus.APPROVED: frozenset(
        {
            ScopeExpansionStatus.APPLIED,
            ScopeExpansionStatus.SUPERSEDED,
        }
    ),
    ScopeExpansionStatus.APPLIED: frozenset(),
    ScopeExpansionStatus.REJECTED: frozenset(),
    ScopeExpansionStatus.SUPERSEDED: frozenset(),
}

# In-force statuses contribute approved_files to the effective scope (#27A section 8).
# These are the only statuses the future overlay may honor; pending/rejected/
# superseded contribute nothing.
_IN_FORCE_STATUSES: frozenset[ScopeExpansionStatus] = frozenset(
    {ScopeExpansionStatus.APPROVED, ScopeExpansionStatus.APPLIED}
)

# Stable eligibility reason identifiers (#27A section 4). Kept stable so callers/UI can
# branch on them without string-matching prose.
SCOPE_EXPANSION_INELIGIBLE_NOT_SCOPE_VIOLATION = "not_scope_violation"
SCOPE_EXPANSION_INELIGIBLE_DIRTY_WORKTREE = "dirty_worktree"
SCOPE_EXPANSION_INELIGIBLE_MANUAL_INTERVENTION = "manual_intervention_needed"
SCOPE_EXPANSION_INELIGIBLE_MISSING_FAILURE_REPORT_ID = "missing_failure_report_id"
SCOPE_EXPANSION_INELIGIBLE_STALE_FAILURE_REPORT_ID = "stale_failure_report_id"
SCOPE_EXPANSION_INELIGIBLE_AMENDMENTS_EXHAUSTED = "scope_amendments_exhausted"
SCOPE_EXPANSION_INELIGIBLE_NO_REQUESTED_FILES = "no_requestable_files"

# Stable approve-and-retry eligibility reason identifiers. These package the
# route/orchestrator guards around an existing pending/approved request; the core
# scope-expansion requestability decision above remains the source of truth for
# clean SCOPE_VIOLATION eligibility.
SCOPE_EXPANSION_APPROVE_INELIGIBLE_PLAN_NOT_APPROVED = "chunk_plan_not_approved"
SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE = "request_not_actionable"
SCOPE_EXPANSION_APPROVE_INELIGIBLE_CHUNK_NOT_FAILED = "chunk_not_failed"
SCOPE_EXPANSION_APPROVE_INELIGIBLE_MISSING_REPORT = "missing_patch_failure_report"
SCOPE_EXPANSION_APPROVE_INELIGIBLE_STALE_REPORT = "stale_failure_report"
SCOPE_EXPANSION_APPROVE_INELIGIBLE_DIRTY_WORKTREE = "dirty_worktree"

# Stable approved-file validation reason identifiers (#27A section 12 / section 22).
SCOPE_EXPANSION_APPROVED_EMPTY = "approved_files_empty"
SCOPE_EXPANSION_APPROVED_INVALID_PATH = "approved_file_invalid_path"
SCOPE_EXPANSION_APPROVED_FORBIDDEN = "approved_file_forbidden"
SCOPE_EXPANSION_APPROVED_NOT_SUBSET = "approved_file_not_in_requested"


class ScopeExpansionValidationError(ValueError):
    """
    Raised when approved scope-expansion files fail validation (#27B).

    Carries a stable ``reason`` identifier so callers can branch deterministically
    without matching prose.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ScopeExpansionRequest(BaseModel):
    """
    Pure data shape for a scope expansion request (#27A section 5).

    This is the model only — no DB table is created in #27B (that is a later
    slice). It never carries file contents, old_string/new_string, secrets, or
    token-like values; only paths, ids, status, audit timestamps, and a sanitized
    decision reason.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: str
    run_id: str
    project_id: str
    chunk_number: int
    # Optimistic-concurrency token tying the request to the exact failure that
    # motivated it (#27A section 5/section 11).
    failure_report_id: str
    # Untrusted: the extra paths the failed attempt tried to touch. Diagnostic
    # display only — never authorization (#27A section 23).
    requested_files: list[str] = Field(default_factory=list)
    # The human-approved expanded allowlist (normalized safe relative paths).
    # Empty until approved. The authoritative contribution to effective scope.
    approved_files: list[str] = Field(default_factory=list)
    status: ScopeExpansionStatus = ScopeExpansionStatus.PENDING
    created_at: str | None = None
    decided_at: str | None = None
    applied_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class ScopeExpansionEligibilityDecision(BaseModel):
    """
    Pure decision about whether a human may request scope expansion (#27A section 4).

    Carries no behavior: it does not mutate state or touch disk/git. ``status_code``
    is a *suggested* HTTP status for a future route (#27E) and is None only when
    ``eligible`` is True. ``requestable_files`` is the normalized, deduplicated,
    denylist-filtered set of paths a human could approve.
    """

    eligible: bool
    reason: str | None = None
    status_code: int | None = None
    failure_type: PatchFailureType | None = None
    amendments_used: int
    max_scope_amendments: int
    requestable_files: list[str] = Field(default_factory=list)


class ScopeExpansionApproveRetryEligibilityDecision(BaseModel):
    """
    Pure decision for acting on an existing scope-expansion request.

    This is the approve-and-retry companion to evaluate_scope_expansion_eligibility:
    callers pass already-loaded request/chunk/report/repo observations and get a
    side-effect-free decision. It does not read DB state, touch git, approve
    files, flip lifecycle status, execute retry, or commit.
    """

    eligible: bool
    reason: str | None = None
    status_code: int | None = None
    is_pending: bool = False
    is_redrive: bool = False
    scope_decision: ScopeExpansionEligibilityDecision | None = None


# ---------------------------------------------------------------------------
# Lifecycle helpers (#27A section 6 / section 14)
# ---------------------------------------------------------------------------


def _coerce_status(status: ScopeExpansionStatus | str) -> ScopeExpansionStatus:
    return status if isinstance(status, ScopeExpansionStatus) else ScopeExpansionStatus(status)


def is_transition_allowed(
    current: ScopeExpansionStatus | str,
    target: ScopeExpansionStatus | str,
) -> bool:
    """True iff moving ``current`` -> ``target`` is a permitted lifecycle edge."""
    return _coerce_status(target) in _ALLOWED_TRANSITIONS[_coerce_status(current)]


def can_approve(status: ScopeExpansionStatus | str) -> bool:
    """
    True iff a request in ``status`` may be *newly* approved.

    Only a pending request can be newly approved. approved/applied/rejected/
    superseded cannot (#27A section 6). Re-driving an already-approved-but-not-applied
    request is a separate, idempotent operation — see can_redrive_retry.
    """
    return _coerce_status(status) == ScopeExpansionStatus.PENDING


def can_redrive_retry(status: ScopeExpansionStatus | str) -> bool:
    """
    True iff an approved-but-not-applied request may be re-driven (#27A section 14).

    Supports crash-window idempotency: if the process crashed after pending ->
    approved but before the retry wrote a fresh attempt/report (status still
    approved, not yet applied), re-clicking approve must re-drive the retry rather
    than reject it as already approved.
    """
    return _coerce_status(status) == ScopeExpansionStatus.APPROVED


def is_in_force(status: ScopeExpansionStatus | str) -> bool:
    """
    True iff a request in ``status`` contributes approved_files to effective
    scope (#27A section 8): approved or applied. Pending/rejected/superseded do not.

    Both approved and applied are in force so effective scope stays stable from
    approval through commit completion (the chunk re-checks scope at commit time
    when the request has already flipped to applied).
    """
    return _coerce_status(status) in _IN_FORCE_STATUSES


# ---------------------------------------------------------------------------
# Path helpers (#27A section 12 / section 22) — write-path safety, not read-path safety
# ---------------------------------------------------------------------------


def _is_absolute_path(original: str, normalized: str) -> bool:
    """True for POSIX-rooted or Windows-drive-absolute paths (pure, OS-stable)."""
    if normalized.startswith("/"):
        return True
    # PureWindowsPath flags a drive-absolute path (e.g. C:\foo) regardless of the
    # host OS, so this stays deterministic in tests on any platform.
    return PureWindowsPath(original).is_absolute()


def normalize_safe_relative_path(path: str) -> str | None:
    """
    Normalize a repo-relative path for scope expansion, or return None if it is
    structurally unsafe (#27A section 22).

    Pure and filesystem-free. Rejects (returns None for): empty paths, absolute
    paths, ``..`` traversal, and glob/wildcard/directory approvals. Does NOT apply
    the forbidden/high-risk denylist — that is is_scope_expansion_forbidden so
    callers can distinguish "malformed path" from "forbidden file".

    Note: symlink-escape detection requires a real repo root and is therefore not
    pure; it is deferred to the route layer (#27E) via
    path_safety.validate_safe_relative_path.
    """
    try:
        normalized = normalize_relative_path(path)
    except Exception:
        return None

    segments = [segment for segment in normalized.split("/") if segment]
    if not segments:
        return None
    if _is_absolute_path(path, normalized):
        return None
    if ".." in normalized.split("/"):
        return None
    if any(meta in normalized for meta in _GLOB_METACHARACTERS):
        return None
    return normalized


def _contains_subsequence(segments: list[str], sub: tuple[str, ...]) -> bool:
    """True iff ``sub`` appears as consecutive elements anywhere in ``segments``."""
    if not sub:
        return False
    for start in range(len(segments) - len(sub) + 1):
        if tuple(segments[start : start + len(sub)]) == sub:
            return True
    return False


def is_scope_expansion_forbidden(path: str) -> bool:
    """
    True iff ``path`` is high-risk and may never be approved for scope expansion
    (#27A section 22). Uses write-path safety, not read-path safety.

    A strict SUPERSET of path_safety.is_forbidden_write_path. The #27 delta — the
    paths is_forbidden_write_path does NOT cover today — is added here:
    pyproject.toml, .github/workflows/*, migrations/* , migration/* ,
    alembic/versions/* . (Already covered by is_forbidden_write_path: .env* ,
    secrets/private, .git/* , requirements.txt, package-lock.json, Dockerfile,
    docker-compose.yml.)

    Returns True (forbidden) for anything that cannot be normalized, failing safe.
    """
    try:
        normalized = normalize_relative_path(path)
    except Exception:
        return True

    if is_forbidden_write_path(normalized):
        return True

    segments = [segment.lower() for segment in normalized.split("/") if segment]
    if not segments:
        return True

    if segments[-1] == "pyproject.toml":
        return True
    if "migrations" in segments or "migration" in segments:
        return True
    if _contains_subsequence(segments, (".github", "workflows")):
        return True
    if _contains_subsequence(segments, ("alembic", "versions")):
        return True
    return False


def filter_requestable_files(paths: Sequence[str]) -> list[str]:
    """
    Normalize, deduplicate, and denylist-filter requested extra files (#27A section 22).

    Returns the paths a human could legitimately approve: structurally safe and
    not high-risk. Unsafe or forbidden paths are silently dropped (requested_files
    is untrusted and diagnostic). Stable order; first occurrence wins. Inputs are
    not mutated.
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        normalized = normalize_safe_relative_path(raw)
        if normalized is None:
            continue
        if is_scope_expansion_forbidden(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def validate_approved_files(
    requested_files: Sequence[str],
    approved_files: Sequence[str],
) -> list[str]:
    """
    Validate a human-approved scope-expansion allowlist (#27A section 12 / section 22).

    Returns the normalized, deduplicated approved paths on success. Raises
    ScopeExpansionValidationError (with a stable ``reason``) on the first failure:

      - empty approved set                      -> approved_files_empty
      - absolute / traversal / glob / malformed -> approved_file_invalid_path
      - high-risk / forbidden (write-path)      -> approved_file_forbidden
      - not an exact subset of requested_files  -> approved_file_not_in_requested

    Pure and filesystem-free. ``approved_files`` must be a subset of the normalized
    ``requested_files`` universe; directory/glob approvals are rejected (each path
    is concrete). Note: this is NOT code approval — it only authorizes retrying
    under a wider allowlist (#27A section 21).
    """
    if not list(approved_files):
        raise ScopeExpansionValidationError(
            SCOPE_EXPANSION_APPROVED_EMPTY,
            "scope_expansion.py: approved file set is empty.",
        )

    # Normalize the requested universe for the subset check. Unnormalizable
    # requested entries are simply absent from the universe (so an approved path
    # can never match them).
    requested_universe: set[str] = set()
    for raw in requested_files:
        normalized = normalize_safe_relative_path(raw)
        if normalized is not None:
            requested_universe.add(normalized)

    result: list[str] = []
    seen: set[str] = set()
    for raw in approved_files:
        normalized = normalize_safe_relative_path(raw)
        if normalized is None:
            raise ScopeExpansionValidationError(
                SCOPE_EXPANSION_APPROVED_INVALID_PATH,
                f"scope_expansion.py: approved path is not a safe relative file: {raw!r}",
            )
        if is_scope_expansion_forbidden(normalized):
            raise ScopeExpansionValidationError(
                SCOPE_EXPANSION_APPROVED_FORBIDDEN,
                f"scope_expansion.py: approved path is high-risk/forbidden: {normalized}",
            )
        if normalized not in requested_universe:
            raise ScopeExpansionValidationError(
                SCOPE_EXPANSION_APPROVED_NOT_SUBSET,
                f"scope_expansion.py: approved path is not in the requested set: {normalized}",
            )
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


# ---------------------------------------------------------------------------
# Effective-scope merge (#27A section 8) — pure helper, NOT wired into chunk_store yet
# ---------------------------------------------------------------------------


def compute_effective_files_expected(
    original_files_expected: Sequence[str],
    approved_extra_files: Sequence[str],
) -> list[str]:
    """
    Merge original (immutable) chunk scope with approved extra files (#27A section 8).

    effective_files_expected = original_files_expected + approved_extra_files

    Rules:
      - Original scope first, approved extras after (stable order preserved).
      - Deduplicate on the normalized path; first occurrence wins.
      - Normalize path separators.
      - Inputs are never mutated.

    Unnormalizable entries are skipped defensively so a single bad path cannot
    crash the merge. This is a PURE helper; #27B does NOT wire it into
    chunk_store.get_chunk_plan_status (that overlay is a later slice, #27C).
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in list(original_files_expected) + list(approved_extra_files):
        try:
            normalized = normalize_relative_path(raw)
        except Exception:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


# ---------------------------------------------------------------------------
# Eligibility (#27A section 4)
# ---------------------------------------------------------------------------


def _is_scope_violation(failure_type: PatchFailureType | str | None) -> bool:
    if failure_type is None:
        return False
    value = failure_type.value if isinstance(failure_type, PatchFailureType) else failure_type
    return value == PatchFailureType.SCOPE_VIOLATION.value


def evaluate_scope_expansion_eligibility(
    *,
    failure_type: PatchFailureType | str | None,
    working_tree_clean: bool,
    manual_intervention_needed: bool,
    amendments_used: int,
    requested_extra_files: Sequence[str],
    report_failure_report_id: str | None = None,
    requested_failure_report_id: str | None = None,
    max_scope_amendments: int = MAX_SCOPE_AMENDMENTS,
) -> ScopeExpansionEligibilityDecision:
    """
    Decide whether a human may request scope expansion for a failed chunk (#27A section 4).

    Pure and deterministic: receives already-computed observations and never reads
    disk, calls git, or mutates state. This is a DISTINCT gate from #26's
    evaluate_patch_retry_eligibility (which hard-rejects SCOPE_VIOLATION); #27 does
    not route through the #26 retry front door.

    Eligible only when ALL hold:
      - failure_type == SCOPE_VIOLATION
      - working_tree_clean is True
      - manual_intervention_needed is False
      - amendments_used < max_scope_amendments
      - at least one requestable (normalized, non-forbidden) extra file exists

    The failure_report_id staleness check is applied only when the caller passes a
    ``requested_failure_report_id`` (the optimistic-concurrency token from a future
    request); when omitted, the core eligibility is evaluated on its own.

    Ineligible results carry a stable ``reason`` and a suggested status:
      - not a SCOPE_VIOLATION        -> 422
      - dirty worktree               -> 409
      - manual intervention needed   -> 409
      - missing failure_report_id    -> 409
      - stale failure_report_id      -> 409
      - scope amendments exhausted   -> 422
      - no requestable extra files   -> 422
    """
    failure_type_enum = (
        failure_type
        if isinstance(failure_type, PatchFailureType) or failure_type is None
        else _as_failure_type(failure_type)
    )
    requestable = filter_requestable_files(requested_extra_files)

    def _ineligible(reason: str, status_code: int) -> ScopeExpansionEligibilityDecision:
        return ScopeExpansionEligibilityDecision(
            eligible=False,
            reason=reason,
            status_code=status_code,
            failure_type=failure_type_enum,
            amendments_used=amendments_used,
            max_scope_amendments=max_scope_amendments,
            requestable_files=requestable,
        )

    if not _is_scope_violation(failure_type):
        return _ineligible(SCOPE_EXPANSION_INELIGIBLE_NOT_SCOPE_VIOLATION, 422)

    if not working_tree_clean:
        return _ineligible(SCOPE_EXPANSION_INELIGIBLE_DIRTY_WORKTREE, 409)

    if manual_intervention_needed:
        return _ineligible(SCOPE_EXPANSION_INELIGIBLE_MANUAL_INTERVENTION, 409)

    if requested_failure_report_id is not None:
        if not report_failure_report_id:
            return _ineligible(SCOPE_EXPANSION_INELIGIBLE_MISSING_FAILURE_REPORT_ID, 409)
        if requested_failure_report_id != report_failure_report_id:
            return _ineligible(SCOPE_EXPANSION_INELIGIBLE_STALE_FAILURE_REPORT_ID, 409)

    if amendments_used >= max_scope_amendments:
        return _ineligible(SCOPE_EXPANSION_INELIGIBLE_AMENDMENTS_EXHAUSTED, 422)

    if not requestable:
        return _ineligible(SCOPE_EXPANSION_INELIGIBLE_NO_REQUESTED_FILES, 422)

    return ScopeExpansionEligibilityDecision(
        eligible=True,
        reason=None,
        status_code=None,
        failure_type=failure_type_enum,
        amendments_used=amendments_used,
        max_scope_amendments=max_scope_amendments,
        requestable_files=requestable,
    )


def evaluate_scope_expansion_approve_retry_eligibility(
    *,
    chunk_plan_status: str,
    request_status: ScopeExpansionStatus | str,
    chunk_status: str,
    has_patch_failure_report: bool,
    report_failure_report_id: str | None,
    request_failure_report_id: str | None,
    working_tree_clean: bool,
    failure_type: PatchFailureType | str | None,
    manual_intervention_needed: bool,
    amendments_used: int,
    requested_extra_files: Sequence[str],
    max_scope_amendments: int = MAX_SCOPE_AMENDMENTS,
) -> ScopeExpansionApproveRetryEligibilityDecision:
    """
    Decide whether an existing request may approve-and-retry or re-drive retry.

    Pure/read-model-safe: all observations are supplied by the caller. The route
    still owns loading fresh state under the repo lock, branch verification,
    approved-file validation, lifecycle mutation, and retry execution.

    Pending requests must still satisfy the full #27 eligibility gate. Approved
    but not applied requests are treated as crash-window re-drives: the approval
    already happened, so cap/requestability checks are not repeated, but the
    current chunk, failure, clean-tree, and branch checks still happen elsewhere
    before retry execution.
    """

    def _decision(
        reason: str,
        status_code: int,
        *,
        is_pending: bool = False,
        is_redrive: bool = False,
        scope_decision: ScopeExpansionEligibilityDecision | None = None,
    ) -> ScopeExpansionApproveRetryEligibilityDecision:
        return ScopeExpansionApproveRetryEligibilityDecision(
            eligible=False,
            reason=reason,
            status_code=status_code,
            is_pending=is_pending,
            is_redrive=is_redrive,
            scope_decision=scope_decision,
        )

    if chunk_plan_status != "approved":
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_PLAN_NOT_APPROVED,
            409,
        )

    try:
        status = _coerce_status(request_status)
    except ValueError:
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE,
            409,
        )

    is_pending = status == ScopeExpansionStatus.PENDING
    is_redrive = status == ScopeExpansionStatus.APPROVED
    if not (is_pending or is_redrive):
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_REQUEST_NOT_ACTIONABLE,
            409,
        )

    if chunk_status != "failed":
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_CHUNK_NOT_FAILED,
            409,
            is_pending=is_pending,
            is_redrive=is_redrive,
        )

    if not has_patch_failure_report:
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_MISSING_REPORT,
            409,
            is_pending=is_pending,
            is_redrive=is_redrive,
        )

    if (
        not report_failure_report_id
        or report_failure_report_id != request_failure_report_id
    ):
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_STALE_REPORT,
            409,
            is_pending=is_pending,
            is_redrive=is_redrive,
        )

    if not working_tree_clean:
        return _decision(
            SCOPE_EXPANSION_APPROVE_INELIGIBLE_DIRTY_WORKTREE,
            409,
            is_pending=is_pending,
            is_redrive=is_redrive,
        )

    if is_pending:
        scope_decision = evaluate_scope_expansion_eligibility(
            failure_type=failure_type,
            working_tree_clean=working_tree_clean,
            manual_intervention_needed=manual_intervention_needed,
            amendments_used=amendments_used,
            requested_extra_files=requested_extra_files,
            report_failure_report_id=report_failure_report_id,
            requested_failure_report_id=request_failure_report_id,
            max_scope_amendments=max_scope_amendments,
        )
        if not scope_decision.eligible:
            return _decision(
                scope_decision.reason or "scope_expansion_ineligible",
                scope_decision.status_code or 409,
                is_pending=True,
                scope_decision=scope_decision,
            )

    return ScopeExpansionApproveRetryEligibilityDecision(
        eligible=True,
        reason=None,
        status_code=None,
        is_pending=is_pending,
        is_redrive=is_redrive,
    )


def _as_failure_type(value: str) -> PatchFailureType | None:
    """Best-effort coercion of a string into PatchFailureType (None if unknown)."""
    try:
        return PatchFailureType(value)
    except ValueError:
        return None
