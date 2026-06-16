"""
prompt_builder.py
Builds compact, role-scoped project memory blocks for prompt injection.

This module only formats already-approved, active project memory facts; it does
not store, mutate, approve, or decide anything. build_project_memory_block is
wired into the triage, planner, and coder roles today (see
backend/pipeline/triage.py, planner.py, coder.py). The reviewer and summary
role policies defined below exist for the /prompt-preview route but are not yet
wired into reviewer/summary execution. See
docs/design/memory-m3-trust-lifecycle.md §8 for the as-built injection map.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.memory.memory_trust import _content_tokens, _jaccard
from backend.memory.memory_retriever import default_memory_retriever
from backend.memory.memory_store import (
    CATEGORY_ORDER,
    DEFAULT_PRIORITY,
    ALLOWED_SCOPES,
)
from backend.pipeline import policy as pipeline_policy

logger = logging.getLogger(__name__)

# Deterministic, execution-time exclusion reasons (M3F2a). These describe why a
# fact was NOT rendered into the block; they are observability metadata only and
# never affect which facts are injected. status_excluded_* reasons are
# intentionally NOT produced here — surfacing inactive facts would require
# widening the active-only query and is deferred to M3F2b.
EXCLUSION_BUDGET_DROPPED = "budget_dropped"
EXCLUSION_CATEGORY_NOT_ALLOWED = "category_not_allowed_for_role"
EXCLUSION_NOT_RELEVANT_TO_REQUEST = "not_relevant_to_request"

MANDATORY_CATEGORIES = frozenset({"security", "forbidden_paths"})
_PATH_TOKEN_RE = re.compile(r"[/._-]+")

# Back-compat alias for callers/tests that inspect prompt_builder directly.
ROLE_TOKEN_BUDGETS = pipeline_policy.MEMORY_ROLE_TOKEN_BUDGETS

# Deterministic role -> allowed category policy (#21F).
#
# Category names are the real memory_facts categories (see
# memory_store.ALLOWED_CATEGORIES). The conceptual policy maps onto them as:
#   safety            -> security, forbidden_paths
#   project_convention-> style
#   file_structure    -> structure
#   tooling           -> stack, deploy
#   api_contract      -> architecture
#   user_preference   -> reviewer_pref
#   rejected_approach / patch_failure_lesson -> other
#     (run-outcome suggestions persist these under "other"/"security"/"test")
#
# Every role always includes the safety categories (security, forbidden_paths).
ROLE_CATEGORIES = {
    # Triage stays intentionally narrow but must still see safety rules.
    "triage": {"security", "forbidden_paths", "stack", "structure", "test", "db"},
    "planner": {
        "security",
        "forbidden_paths",
        "stack",
        "db",
        "test",
        "structure",
        "architecture",
        "style",
        "deploy",
        "reviewer_pref",
        "other",
    },
    "architect": {
        "security",
        "forbidden_paths",
        "stack",
        "db",
        "test",
        "structure",
        "architecture",
        "style",
        "deploy",
    },
    "coder": {
        "security",
        "forbidden_paths",
        "stack",
        "db",
        "test",
        "structure",
        "architecture",
        "style",
        "deploy",
        "reviewer_pref",
        "other",
    },
    # Reviewer is focused (not "everything"): safety, design/contract, conventions,
    # tests, deployment, user prefs, and rejected-approach/lesson notes ("other").
    "reviewer": {
        "security",
        "forbidden_paths",
        "architecture",
        "test",
        "deploy",
        "style",
        "reviewer_pref",
        "other",
    },
    "summary": {"security", "forbidden_paths", "stack", "db", "test", "deploy"},
    "default": {
        "security",
        "forbidden_paths",
        "stack",
        "db",
        "test",
        "structure",
        "architecture",
        "style",
        "deploy",
        "other",
    },
}


def _estimate_tokens(text_value: str) -> int:
    return pipeline_policy.estimate_memory_tokens(text_value)


def _role_key(role: str | None) -> str:
    value = (role or "default").strip().lower()
    return value if value in ROLE_CATEGORIES else "default"


def _category_rank(category: str, role_key: str) -> int:
    if role_key == "reviewer" and category == "reviewer_pref":
        return 2
    if role_key == "reviewer" and category not in {"security", "forbidden_paths"}:
        base = CATEGORY_ORDER.get(category, CATEGORY_ORDER["other"])
        return base + 1 if base >= 2 else base
    return CATEGORY_ORDER.get(category, CATEGORY_ORDER["other"])


def _scope_rank(scope: str, preferred_scopes: set[str]) -> int:
    if scope == "global":
        return 0
    if scope in preferred_scopes:
        return 1
    return 2


def _is_mandatory_row(row: dict) -> bool:
    """
    Whether a row belongs to the un-droppable mandatory tier.

    Always true for safety facts (security / forbidden_paths). Additionally true
    for human-pinned facts (priority at/below the pin threshold) — but ONLY while
    ``MEMORY_RELEVANCE_OMISSION_ENABLED`` is on (Row 12 PR-C). With the flag off
    this reduces to exactly the PR-B safety-only mandatory tier, so a low-priority
    fact stays an ordinary, droppable relevance fact. Pinning rides the existing
    priority field; it is request-context-independent, so it applies to every
    caller. Mandatory rows are never scored, omitted, or budget-dropped.
    """
    if (row.get("category") or "other") in MANDATORY_CATEGORIES:
        return True
    if pipeline_policy.MEMORY_RELEVANCE_OMISSION_ENABLED and (
        int(row.get("priority") or DEFAULT_PRIORITY)
        <= pipeline_policy.MEMORY_PIN_PRIORITY_THRESHOLD
    ):
        return True
    return False


def _memory_row_sort_key(
    row: dict,
    role_key: str,
    preferred_scopes: set[str],
) -> tuple[int, int, int, str]:
    return (
        _category_rank(row.get("category") or "other", role_key),
        _scope_rank(row.get("scope") or "global", preferred_scopes),
        int(row.get("priority") or DEFAULT_PRIORITY),
        row.get("created_at") or "",
    )


def _path_tokens(paths: tuple[str, ...]) -> set[str]:
    tokens: set[str] = set()
    for path in paths:
        for token in _PATH_TOKEN_RE.split(str(path).lower()):
            if token:
                tokens.add(token)
    return tokens


def _request_tokens(request_context: RequestContext) -> set[str]:
    return _content_tokens(
        " ".join(
            part
            for part in (
                request_context.title,
                request_context.description,
                request_context.steer_text,
            )
            if part
        )
    )


def _partition_relevance_rows(
    rows: list[dict],
    request_context: RequestContext | None,
    role_key: str,
    preferred_scopes: set[str],
    relevance_scores: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Order the non-mandatory relevance tier by request relevance and, when
    request-aware omission is enabled, split off the zero-signal facts.

    Returns ``(kept_rows, omitted_rows)`` with ``kept_rows`` in render order.
    ``omitted_rows`` is always empty (Row 12 PR-B ordering-only behavior) unless
    ALL of:
      - ``MEMORY_RELEVANCE_OMISSION_ENABLED`` is on,
      - ``request_context`` carries usable signal,
      - at least one relevance fact carries signal — the all-zero degeneracy
        guard: if none do, nothing is omitted (matching PR-B), and
      - the relevance-candidate count exceeds the small-store grace threshold.
    A kept fact may still be ``budget_dropped`` downstream; an omitted fact is
    excluded for relevance (``not_relevant_to_request``) even with budget to
    spare. Kept-row ordering is identical to PR-B; omission only removes
    zero-signal rows, it never reorders within the kept set.
    """
    fts_scores = relevance_scores or {}
    if request_context is None or not rows:
        return rows, []

    path_tokens = _path_tokens(request_context.files_expected)
    request_tokens = _request_tokens(request_context)
    if not path_tokens and not request_tokens and not fts_scores:
        return rows, []

    scored_rows: list[tuple[dict, int, float, float]] = []
    has_relevance_signal = False
    for row in rows:
        fact_tokens = _content_tokens(row.get("content") or "")
        path_overlap = len(fact_tokens & path_tokens)
        token_overlap = _jaccard(fact_tokens, request_tokens)
        fts_score = float(fts_scores.get(str(row.get("id") or ""), 0.0))
        if path_overlap or token_overlap or fts_score:
            has_relevance_signal = True
        scored_rows.append((row, path_overlap, token_overlap, fts_score))

    if not has_relevance_signal:
        return rows, []

    def sort_key(item: tuple[dict, int, float, float]) -> tuple:
        row, path_overlap, token_overlap, fts_score = item
        if fts_scores:
            fused_signal = (
                path_overlap
                + token_overlap
                + (fts_score * pipeline_policy.MEMORY_FTS_RELEVANCE_WEIGHT)
            )
            return (
                -fused_signal,
                -path_overlap,
                -token_overlap,
                *_memory_row_sort_key(row, role_key, preferred_scopes),
            )
        return (
            -path_overlap,
            -token_overlap,
            *_memory_row_sort_key(row, role_key, preferred_scopes),
        )

    ordered = sorted(scored_rows, key=sort_key)

    # Small-store grace (D5): omission engages only above the policy threshold,
    # counted over these non-mandatory relevance candidates only. Below/at it we
    # keep PR-B's "order, never omit" behavior.
    omission_active = (
        pipeline_policy.MEMORY_RELEVANCE_OMISSION_ENABLED
        and len(rows) > pipeline_policy.MEMORY_SMALL_STORE_GRACE_THRESHOLD
    )
    if not omission_active:
        return [row for row, _, _, _ in ordered], []

    kept: list[dict] = []
    omitted: list[dict] = []
    for row, path_overlap, token_overlap, fts_score in ordered:
        if path_overlap or token_overlap or fts_score:
            kept.append(row)
        else:
            omitted.append(row)
    return kept, omitted


@dataclass(frozen=True)
class RequestContext:
    title: str | None = None
    description: str | None = None
    files_expected: tuple[str, ...] = ()
    steer_text: str | None = None


class MandatoryMemoryBudgetExceeded(RuntimeError):
    """Raised when the mandatory tier (safety + pinned) alone exceeds the budget."""


@dataclass(frozen=True)
class InjectedMemoryEntry:
    """
    One memory fact as it was considered for a role's prompt block.

    ``exclusion_reason`` is None for included entries. For excluded entries it is
    a deterministic, execution-time reason (EXCLUSION_BUDGET_DROPPED or
    EXCLUSION_CATEGORY_NOT_ALLOWED in M3F2a).
    """
    fact_id: str | None
    content: str
    content_hash: str | None
    category: str
    scope: str
    priority: int
    status_at_injection: str
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class MemoryBlockBuildResult:
    """
    The exact memory block string plus the structured detail of what went into
    it, produced by a SINGLE computation so the two can never diverge.

    included_entries: facts rendered into ``block``, in render order.
    excluded_entries: active, non-stale facts that were considered but NOT
        rendered, each carrying a deterministic ``exclusion_reason`` (M3F2a):
          - EXCLUSION_BUDGET_DROPPED: in-policy fact dropped because the token
            budget filled — the "a fact was silently left out" signal.
          - EXCLUSION_CATEGORY_NOT_ALLOWED: active fact whose category is not in
            the role's policy (surfaced from the same query, zero extra cost).
        Facts excluded by status (stale/archived/historical) are NOT listed here
        — that requires widening the active-only query and is deferred to M3F2b.
    """
    block: str
    role: str | None
    token_budget: int
    category_policy: tuple[str, ...]
    included_entries: tuple[InjectedMemoryEntry, ...] = ()
    excluded_entries: tuple[InjectedMemoryEntry, ...] = ()

    @property
    def included_count(self) -> int:
        return len(self.included_entries)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_entries)


def _row_to_entry(
    row: dict, exclusion_reason: str | None = None
) -> InjectedMemoryEntry:
    return InjectedMemoryEntry(
        fact_id=row.get("id"),
        content=row.get("content") or "",
        content_hash=row.get("content_hash"),
        category=row.get("category") or "other",
        scope=row.get("scope") or "global",
        priority=int(row.get("priority") or DEFAULT_PRIORITY),
        status_at_injection=row.get("status") or "active",
        exclusion_reason=exclusion_reason,
    )


def _excluded_sort_key(row: dict) -> tuple:
    """Deterministic ordering for surfaced exclusions (display/provenance only)."""
    return (
        row.get("category") or "other",
        row.get("scope") or "global",
        int(row.get("priority") or DEFAULT_PRIORITY),
        row.get("created_at") or "",
        str(row.get("id") or ""),
    )


def _resolve_role_context_window(role_key: str) -> int:
    from backend.llm.role_config import (
        DEFAULT_MODEL,
        Role,
        resolve_context_window,
        resolve_role_config,
    )

    try:
        role = Role(role_key)
    except ValueError:
        return resolve_context_window(DEFAULT_MODEL)
    return resolve_context_window(resolve_role_config(role).model)


def _resolve_budget(role_key: str, token_budget: int | None) -> int:
    if token_budget is not None:
        return token_budget
    context_window = None
    if pipeline_policy.MEMORY_ADAPTIVE_BUDGET_ENABLED:
        context_window = _resolve_role_context_window(role_key)
    return pipeline_policy.resolve_memory_token_budget(
        role_key,
        context_window=context_window,
    )


def build_project_memory_block(
    project_id: str,
    role: str | None = None,
    project_name: str | None = None,
    token_budget: int | None = None,
    scopes: list[str] | None = None,
) -> str:
    """
    Build the role-scoped memory block string.

    Byte-identical to its historical output for the same inputs; this now
    delegates to build_project_memory_block_detailed and returns only the block
    string, so existing callers are unaffected.
    """
    return build_project_memory_block_detailed(
        project_id=project_id,
        role=role,
        project_name=project_name,
        token_budget=token_budget,
        scopes=scopes,
    ).block


def build_project_memory_block_detailed(
    project_id: str,
    role: str | None = None,
    project_name: str | None = None,
    token_budget: int | None = None,
    scopes: list[str] | None = None,
    request_context: RequestContext | None = None,
) -> MemoryBlockBuildResult:
    """
    Pure builder returning the block string AND the structured injection detail
    from one computation. Performs NO writes and NO repo/LLM access; it only
    reads already-approved active memory facts (exactly as before). The ``block``
    is identical to what build_project_memory_block returns.

    ``request_context`` reorders the non-mandatory relevance tier when it has
    usable request signal. When ``MEMORY_RELEVANCE_OMISSION_ENABLED`` is on (Row
    12 PR-C) it additionally (a) routes human-pinned facts (``priority`` at/below
    ``MEMORY_PIN_PRIORITY_THRESHOLD``) into the mandatory tier, and (b) above the
    small-store grace threshold, omits zero-signal relevance facts with the
    ``not_relevant_to_request`` reason. With the flag off (the default) this is
    byte-identical to PR-B. The mandatory tier (safety + pinned) is never scored,
    reordered, omitted, or budget-dropped to make room for relevance facts.
    """
    role_key = _role_key(role)
    categories = ROLE_CATEGORIES[role_key]
    category_policy = tuple(sorted(categories))

    if not project_id or not str(project_id).strip():
        logger.warning(
            "prompt_builder.py: build_project_memory_block called without project_id"
        )
        return MemoryBlockBuildResult(
            block="",
            role=role,
            token_budget=0,
            category_policy=category_policy,
        )

    project_id = str(project_id).strip()
    budget = _resolve_budget(role_key, token_budget)
    preferred_scopes = {
        scope for scope in (scopes or [])
        if scope in ALLOWED_SCOPES and scope != "global"
    }

    candidates = default_memory_retriever().retrieve_candidates(
        project_id,
        categories,
        request_context=request_context,
    )
    in_policy_rows = candidates.in_policy_rows
    out_of_policy_rows = candidates.out_of_policy_rows

    # Category-policy exclusions: active facts whose category is outside the
    # role's policy. Surfaced read-only from the SAME query; never rendered and
    # never able to influence included selection or the block bytes.
    category_excluded_entries = [
        _row_to_entry(row, EXCLUSION_CATEGORY_NOT_ALLOWED)
        for row in sorted(out_of_policy_rows, key=_excluded_sort_key)
    ]

    if not in_policy_rows:
        return MemoryBlockBuildResult(
            block="",
            role=role,
            token_budget=budget,
            category_policy=category_policy,
            included_entries=(),
            excluded_entries=tuple(category_excluded_entries),
        )

    in_policy_rows.sort(
        key=lambda row: _memory_row_sort_key(row, role_key, preferred_scopes)
    )

    selected_lines: list[str] = []
    included_entries: list[InjectedMemoryEntry] = []
    budget_excluded_entries: list[InjectedMemoryEntry] = []
    used_tokens = 0
    # Mandatory tier = safety categories always, plus human-pinned facts when the
    # Row 12 PR-C flag is on (see _is_mandatory_row). With the flag off this is
    # exactly the PR-B safety-only tier. Mandatory rows are never scored, omitted,
    # or budget-dropped.
    mandatory_rows = [row for row in in_policy_rows if _is_mandatory_row(row)]
    relevance_rows = [row for row in in_policy_rows if not _is_mandatory_row(row)]
    relevance_rows, not_relevant_rows = _partition_relevance_rows(
        relevance_rows,
        request_context,
        role_key,
        preferred_scopes,
        relevance_scores=candidates.relevance_scores,
    )
    not_relevant_entries = [
        _row_to_entry(row, EXCLUSION_NOT_RELEVANT_TO_REQUEST)
        for row in not_relevant_rows
    ]

    for row in mandatory_rows:
        category = row.get("category") or "other"
        scope = row.get("scope") or "global"
        line = f"[{category}/{scope}] {row['content']}"
        line_tokens = _estimate_tokens(line)
        separator_tokens = 1 if selected_lines else 0
        selected_lines.append(line)
        included_entries.append(_row_to_entry(row))
        used_tokens += separator_tokens + line_tokens

    if used_tokens > budget:
        raise MandatoryMemoryBudgetExceeded(
            "memory prompt mandatory tier (safety + pinned) exceeds token "
            f"budget: {used_tokens} / {budget} tokens"
        )

    for row in relevance_rows:
        category = row.get("category") or "other"
        scope = row.get("scope") or "global"
        line = f"[{category}/{scope}] {row['content']}"
        line_tokens = _estimate_tokens(line)
        separator_tokens = 1 if selected_lines else 0
        if used_tokens + separator_tokens + line_tokens > budget:
            budget_excluded_entries.append(
                _row_to_entry(row, EXCLUSION_BUDGET_DROPPED)
            )
            continue
        selected_lines.append(line)
        included_entries.append(_row_to_entry(row))
        used_tokens += separator_tokens + line_tokens

    # Display/provenance ordering only — it never affects which facts were
    # injected: budget drops first (the actionable "silently left out" signal),
    # then relevance omissions (not_relevant_to_request), then the category-policy
    # exclusions.
    excluded_entries = (
        budget_excluded_entries + not_relevant_entries + category_excluded_entries
    )

    if not selected_lines:
        return MemoryBlockBuildResult(
            block="",
            role=role,
            token_budget=budget,
            category_policy=category_policy,
            included_entries=(),
            excluded_entries=tuple(excluded_entries),
        )

    generated = datetime.now(timezone.utc).isoformat()
    project_label = project_name or project_id
    block = "\n".join([
        "=== PROJECT MEMORY (advisory; source code wins on conflict) ===",
        f"Project: {project_label}",
        f"Generated: {generated}",
        f"Entries: {len(selected_lines)} active shown",
        f"Budget used: {used_tokens} / {budget} tokens",
        "",
        *selected_lines,
        "",
        (
            "Memory is advisory context only. If a memory entry conflicts with "
            "the current source code, the user's explicit instruction, the "
            "project's tests, or Pipewright's safety rules, follow the source "
            "code / user instruction / tests / safety rules and suggest a "
            "memory update."
        ),
        "=== END PROJECT MEMORY ===",
    ])
    return MemoryBlockBuildResult(
        block=block,
        role=role,
        token_budget=budget,
        category_policy=category_policy,
        included_entries=tuple(included_entries),
        excluded_entries=tuple(excluded_entries),
    )
