"""
prompt_builder.py
Builds compact project memory blocks for future prompt injection.

This module does not wire memory into planner/coder/triage. It only formats
already-approved project memory facts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from backend.db.database import engine
from backend.memory.memory_store import (
    CATEGORY_ORDER,
    DEFAULT_PRIORITY,
    ALLOWED_SCOPES,
)

logger = logging.getLogger(__name__)

ROLE_TOKEN_BUDGETS = {
    "triage": 300,
    "planner": 1500,
    "architect": 1500,
    "coder": 1500,
    "reviewer": 1500,
    "summary": 800,
}

ROLE_CATEGORIES = {
    "triage": {"stack", "structure", "test", "db"},
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
    },
    "reviewer": set(CATEGORY_ORDER),
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
            SELECT content, category, scope, priority, created_at
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


def build_project_memory_block(
    project_id: str,
    role: str | None = None,
    project_name: str | None = None,
    token_budget: int | None = None,
    scopes: list[str] | None = None,
) -> str:
    if not project_id or not str(project_id).strip():
        logger.warning(
            "prompt_builder.py: build_project_memory_block called without project_id"
        )
        return ""

    project_id = str(project_id).strip()
    role_key = _role_key(role)
    budget = token_budget if token_budget is not None else ROLE_TOKEN_BUDGETS.get(
        role_key,
        1500,
    )
    categories = ROLE_CATEGORIES[role_key]
    preferred_scopes = {
        scope for scope in (scopes or [])
        if scope in ALLOWED_SCOPES and scope != "global"
    }

    rows = _load_active_memory_rows(project_id, categories)
    if not rows:
        return ""

    rows.sort(key=lambda row: (
        _category_rank(row.get("category") or "other", role_key),
        _scope_rank(row.get("scope") or "global", preferred_scopes),
        int(row.get("priority") or DEFAULT_PRIORITY),
        row.get("created_at") or "",
    ))

    selected_lines: list[str] = []
    used_tokens = 0
    for row in rows:
        category = row.get("category") or "other"
        scope = row.get("scope") or "global"
        line = f"[{category}/{scope}] {row['content']}"
        line_tokens = _estimate_tokens(line)
        separator_tokens = 1 if selected_lines else 0
        if used_tokens + separator_tokens + line_tokens > budget:
            continue
        selected_lines.append(line)
        used_tokens += separator_tokens + line_tokens

    if not selected_lines:
        return ""

    generated = datetime.now(timezone.utc).isoformat()
    project_label = project_name or project_id
    return "\n".join([
        "=== PROJECT MEMORY (advisory; source code wins on conflict) ===",
        f"Project: {project_label}",
        f"Generated: {generated}",
        f"Entries: {len(selected_lines)} active shown",
        f"Budget used: {used_tokens} / {budget} tokens",
        "",
        *selected_lines,
        "",
        (
            "If a memory entry conflicts with the current source code or the "
            "user's explicit instruction, follow the source code / user "
            "instruction and suggest a memory update."
        ),
        "=== END PROJECT MEMORY ===",
    ])
