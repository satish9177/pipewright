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
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from backend.db.database import engine
from backend.memory.memory_store import (
    CATEGORY_ORDER,
    DEFAULT_PRIORITY,
    ALLOWED_SCOPES,
)

logger = logging.getLogger(__name__)

# Per-role token budgets. Deliberately conservative: a role should receive only
# enough advisory memory to be useful, never a large block that competes with the
# current request / source code.
ROLE_TOKEN_BUDGETS = {
    "triage": 400,
    "planner": 1200,
    "architect": 1200,
    "coder": 1200,
    "reviewer": 800,
    "summary": 800,
}

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
    return max(1, (len(text_value) + 3) // 4)


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


def _load_active_memory_rows(project_id: str, categories: set[str]) -> list[dict]:
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, content, content_hash, category, scope, priority, status,
                   created_at
            FROM memory_facts
            WHERE project_id = :project_id
              AND is_stale = 0
              AND status = 'active'
        """), {"project_id": project_id})
        rows = [dict(row._mapping) for row in result.fetchall()]
    return [
        row for row in rows
        if (row.get("category") or "other") in categories
    ]


@dataclass(frozen=True)
class InjectedMemoryEntry:
    """One memory fact as it was considered for a role's prompt block."""
    fact_id: str | None
    content: str
    content_hash: str | None
    category: str
    scope: str
    priority: int
    status_at_injection: str


@dataclass(frozen=True)
class MemoryBlockBuildResult:
    """
    The exact memory block string plus the structured detail of what went into
    it, produced by a SINGLE computation so the two can never diverge.

    included_entries: facts rendered into ``block``, in render order.
    excluded_entries: in-policy active facts dropped ONLY because the token
        budget filled — the actionable "a fact was silently left out" signal.
        Facts excluded by role category policy or by status are not listed here.
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


def _row_to_entry(row: dict) -> InjectedMemoryEntry:
    return InjectedMemoryEntry(
        fact_id=row.get("id"),
        content=row.get("content") or "",
        content_hash=row.get("content_hash"),
        category=row.get("category") or "other",
        scope=row.get("scope") or "global",
        priority=int(row.get("priority") or DEFAULT_PRIORITY),
        status_at_injection=row.get("status") or "active",
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
) -> MemoryBlockBuildResult:
    """
    Pure builder returning the block string AND the structured injection detail
    from one computation. Performs NO writes and NO repo/LLM access; it only
    reads already-approved active memory facts (exactly as before). The ``block``
    is identical to what build_project_memory_block returns.
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
    budget = token_budget if token_budget is not None else ROLE_TOKEN_BUDGETS.get(
        role_key,
        1500,
    )
    preferred_scopes = {
        scope for scope in (scopes or [])
        if scope in ALLOWED_SCOPES and scope != "global"
    }

    rows = _load_active_memory_rows(project_id, categories)
    if not rows:
        return MemoryBlockBuildResult(
            block="",
            role=role,
            token_budget=budget,
            category_policy=category_policy,
        )

    rows.sort(key=lambda row: (
        _category_rank(row.get("category") or "other", role_key),
        _scope_rank(row.get("scope") or "global", preferred_scopes),
        int(row.get("priority") or DEFAULT_PRIORITY),
        row.get("created_at") or "",
    ))

    selected_lines: list[str] = []
    included_entries: list[InjectedMemoryEntry] = []
    excluded_entries: list[InjectedMemoryEntry] = []
    used_tokens = 0
    for row in rows:
        category = row.get("category") or "other"
        scope = row.get("scope") or "global"
        line = f"[{category}/{scope}] {row['content']}"
        line_tokens = _estimate_tokens(line)
        separator_tokens = 1 if selected_lines else 0
        if used_tokens + separator_tokens + line_tokens > budget:
            excluded_entries.append(_row_to_entry(row))
            continue
        selected_lines.append(line)
        included_entries.append(_row_to_entry(row))
        used_tokens += separator_tokens + line_tokens

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
