"""
Memory-selection scaffolding and relevance-ordering tests.

request_context=None preserves the legacy memory block bytes. A populated
request_context can reorder only the non-mandatory relevance tier; facts are
not omitted for relevance in this slice.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.llm.role_config import (
    DEFAULT_CONTEXT_WINDOW,
    MODEL_CONTEXT_WINDOWS,
    resolve_context_window,
)
from backend.memory.memory_store import add_fact
from backend.memory.prompt_builder import (
    EXCLUSION_BUDGET_DROPPED,
    EXCLUSION_CATEGORY_NOT_ALLOWED,
    EXCLUSION_NOT_RELEVANT_TO_REQUEST,
    MandatoryMemoryBudgetExceeded,
    RequestContext,
    build_project_memory_block_detailed,
)
from backend.pipeline import policy

pytestmark = pytest.mark.unit


@pytest.fixture()
def project_ids():
    ids: list[str] = []
    yield ids
    with engine.begin() as conn:
        for project_id in ids:
            conn.execute(
                text("DELETE FROM memory_facts WHERE project_id = :p"),
                {"p": project_id},
            )


def _new_project(project_ids, label="r12a"):
    project_id = f"{label}-{uuid.uuid4().hex}"
    project_ids.append(project_id)
    return project_id


def _strip_generated(block: str) -> str:
    return "\n".join(
        line for line in block.splitlines() if not line.startswith("Generated:")
    )


def _line_tokens(category: str, scope: str, content: str) -> int:
    return policy.estimate_memory_tokens(f"[{category}/{scope}] {content}")


def test_request_context_none_keeps_legacy_block_bytes(project_ids):
    project_id = _new_project(project_ids)
    add_fact(project_id, "Db fact delta.", category="db")
    add_fact(project_id, "Stack fact gamma.", category="stack", scope="backend")
    add_fact(project_id, "Security fact alpha.", category="security")
    add_fact(project_id, "Forbidden path fact beta.", category="forbidden_paths")
    add_fact(
        project_id,
        "Reviewer preference hidden.",
        category="reviewer_pref",
    )

    result = build_project_memory_block_detailed(
        project_id=project_id,
        project_name="Demo",
        request_context=None,
    )

    assert _strip_generated(result.block) == "\n".join([
        "=== PROJECT MEMORY (advisory; source code wins on conflict) ===",
        "Project: Demo",
        "Entries: 4 active shown",
        "Budget used: 42 / 1500 tokens",
        "",
        "[security/global] Security fact alpha.",
        "[forbidden_paths/global] Forbidden path fact beta.",
        "[stack/backend] Stack fact gamma.",
        "[db/global] Db fact delta.",
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
    assert [
        (entry.content, entry.category, entry.scope, entry.priority)
        for entry in result.included_entries
    ] == [
        ("Security fact alpha.", "security", "global", 100),
        ("Forbidden path fact beta.", "forbidden_paths", "global", 100),
        ("Stack fact gamma.", "stack", "backend", 100),
        ("Db fact delta.", "db", "global", 100),
    ]
    assert [
        (entry.content, entry.exclusion_reason)
        for entry in result.excluded_entries
    ] == [("Reviewer preference hidden.", EXCLUSION_CATEGORY_NOT_ALLOWED)]


def test_populated_request_context_reorders_relevance_tier_without_omission(
    project_ids,
):
    project_id = _new_project(project_ids)
    add_fact(
        project_id,
        "Backend uses FastAPI routers.",
        category="stack",
        scope="backend",
    )
    add_fact(
        project_id,
        "Tests use pytest fixtures.",
        category="test",
    )
    add_fact(
        project_id,
        "Frontend App tsx component lives in frontend src.",
        category="structure",
        scope="frontend",
    )

    context = RequestContext(
        title="Update the frontend App view",
        description="Change App component rendering.",
        files_expected=("frontend/src/App.tsx",),
        steer_text="Prefer the smallest edit.",
    )
    without_context = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=200,
    )
    with_context = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=200,
        request_context=context,
    )

    assert {entry.content for entry in with_context.included_entries} == {
        entry.content for entry in without_context.included_entries
    }
    assert [entry.content for entry in without_context.included_entries] == [
        "Backend uses FastAPI routers.",
        "Tests use pytest fixtures.",
        "Frontend App tsx component lives in frontend src.",
    ]
    assert [
        (entry.content, entry.category, entry.scope, entry.priority)
        for entry in with_context.included_entries
    ] == [
        (
            "Frontend App tsx component lives in frontend src.",
            "structure",
            "frontend",
            100,
        ),
        ("Backend uses FastAPI routers.", "stack", "backend", 100),
        ("Tests use pytest fixtures.", "test", "global", 100),
    ]
    assert with_context.excluded_entries == ()


def test_relevance_ordering_keeps_high_signal_fact_under_binding_budget(
    project_ids,
):
    project_id = _new_project(project_ids)
    zero_overlap = add_fact(
        project_id,
        "Style rule: keep helper naming concise.",
        category="style",
        priority=100,
    )
    high_relevance = add_fact(
        project_id,
        "Style rule: frontend App tsx rendering stays simple.",
        category="style",
        priority=100,
    )
    budget = _line_tokens("style", "global", high_relevance["content"])

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=budget,
        request_context=RequestContext(
            title="Update App rendering",
            files_expected=("frontend/src/App.tsx",),
        ),
    )

    assert [
        (entry.content, entry.category, entry.scope, entry.priority)
        for entry in result.included_entries
    ] == [
        (
            "Style rule: frontend App tsx rendering stays simple.",
            "style",
            "global",
            100,
        )
    ]
    assert [
        (entry.content, entry.exclusion_reason)
        for entry in result.excluded_entries
    ] == [(zero_overlap["content"], EXCLUSION_BUDGET_DROPPED)]
    assert EXCLUSION_NOT_RELEVANT_TO_REQUEST not in {
        entry.exclusion_reason for entry in result.excluded_entries
    }


def test_all_zero_request_overlap_preserves_legacy_order(project_ids):
    project_id = _new_project(project_ids)
    add_fact(project_id, "Backend uses FastAPI routers.", category="stack")
    add_fact(project_id, "Tests use pytest fixtures.", category="test")
    add_fact(project_id, "Structure rule: services stay layered.", category="structure")

    without_context = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=200,
    )
    with_context = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=200,
        request_context=RequestContext(
            title="Banana orchard change",
            description="Adjust pear grove notes.",
            files_expected=("docs/kiwi.md",),
            steer_text="Mention melon only.",
        ),
    )

    assert _strip_generated(with_context.block) == _strip_generated(
        without_context.block
    )
    assert with_context.included_entries == without_context.included_entries
    assert with_context.excluded_entries == without_context.excluded_entries


def test_relevance_ordering_is_deterministic_and_ties_use_legacy_key(
    project_ids,
):
    project_id = _new_project(project_ids)
    add_fact(
        project_id,
        "Style fact alpha frontend.",
        category="style",
        priority=200,
    )
    add_fact(
        project_id,
        "Style fact beta frontend.",
        category="style",
        priority=100,
    )
    context = RequestContext(title="frontend")

    first = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=200,
        request_context=context,
    )
    second = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=200,
        request_context=context,
    )

    assert [entry.content for entry in first.included_entries] == [
        "Style fact beta frontend.",
        "Style fact alpha frontend.",
    ]
    assert first.included_entries == second.included_entries
    assert first.excluded_entries == second.excluded_entries


def test_mandatory_safety_facts_are_never_budget_dropped(project_ids):
    project_id = _new_project(project_ids)
    security = add_fact(
        project_id,
        "Never leak tokens.",
        category="security",
        priority=25,
    )
    forbidden = add_fact(
        project_id,
        "Keep secret files untouched.",
        category="forbidden_paths",
        priority=25,
    )
    add_fact(
        project_id,
        "Stack fact: backend uses FastAPI here.",
        category="stack",
        priority=1,
    )
    mandatory_budget = (
        _line_tokens("security", "global", security["content"])
        + 1
        + _line_tokens("forbidden_paths", "global", forbidden["content"])
    )

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=mandatory_budget,
        request_context=RequestContext(
            title="Change backend FastAPI behavior",
            description="This matches the non-mandatory stack fact.",
            files_expected=("backend/app.py",),
        ),
    )

    assert [
        (entry.content, entry.category, entry.exclusion_reason)
        for entry in result.included_entries
    ] == [
        ("Never leak tokens.", "security", None),
        ("Keep secret files untouched.", "forbidden_paths", None),
    ]
    assert all(
        entry.category not in {"security", "forbidden_paths"}
        for entry in result.excluded_entries
    )
    assert [
        (entry.content, entry.category, entry.exclusion_reason)
        for entry in result.excluded_entries
    ] == [
        (
            "Stack fact: backend uses FastAPI here.",
            "stack",
            EXCLUSION_BUDGET_DROPPED,
        )
    ]


def test_mandatory_safety_overflow_is_loud(project_ids):
    project_id = _new_project(project_ids)
    security = add_fact(
        project_id,
        "Never leak tokens.",
        category="security",
    )
    forbidden = add_fact(
        project_id,
        "Keep secret files untouched.",
        category="forbidden_paths",
    )
    mandatory_budget = (
        _line_tokens("security", "global", security["content"])
        + 1
        + _line_tokens("forbidden_paths", "global", forbidden["content"])
    )

    with pytest.raises(MandatoryMemoryBudgetExceeded, match="mandatory safety"):
        build_project_memory_block_detailed(
            project_id=project_id,
            role="planner",
            token_budget=mandatory_budget - 1,
        )


def test_not_relevant_reason_is_defined_but_never_emitted(project_ids):
    project_id = _new_project(project_ids)
    for index in range(3):
        add_fact(
            project_id,
            f"Style fact number {index}: keep helpers compact.",
            category="style",
            priority=100 + index,
        )

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=12,
        request_context=RequestContext(title="Unrelated backend change"),
    )

    assert EXCLUSION_NOT_RELEVANT_TO_REQUEST == "not_relevant_to_request"
    assert result.excluded_entries
    assert {
        entry.exclusion_reason for entry in result.excluded_entries
    } == {EXCLUSION_BUDGET_DROPPED}
    assert EXCLUSION_NOT_RELEVANT_TO_REQUEST not in {
        entry.exclusion_reason for entry in result.excluded_entries
    }


def test_static_memory_budgets_remain_effective_when_adaptive_flag_is_off(
    monkeypatch,
):
    monkeypatch.setattr(policy, "MEMORY_ADAPTIVE_BUDGET_ENABLED", False)

    assert policy.resolve_memory_token_budget("triage", context_window=1_000_000) == 400
    assert policy.resolve_memory_token_budget("planner", context_window=1_000_000) == 1200
    assert policy.resolve_memory_token_budget("unknown", context_window=1_000_000) == 1500


def test_adaptive_memory_budget_flag_stays_off_by_default():
    assert policy.MEMORY_ADAPTIVE_BUDGET_ENABLED is False


def test_adaptive_memory_budget_formula_is_clamped_when_flag_is_on(monkeypatch):
    monkeypatch.setattr(policy, "MEMORY_ADAPTIVE_BUDGET_ENABLED", True)
    monkeypatch.setattr(policy, "MEMORY_BUDGET_FLOOR", 100)
    monkeypatch.setattr(policy, "MEMORY_BUDGET_CEILING", 500)
    monkeypatch.setattr(policy, "MEMORY_ROLE_BUDGET_SHARE", 0.10)
    monkeypatch.setattr(policy, "MEMORY_TOKEN_ESTIMATOR_MARGIN", 2.0)

    assert policy.resolve_memory_token_budget("planner", context_window=1_000) == 100
    assert policy.resolve_memory_token_budget("planner", context_window=6_000) == 300
    assert policy.resolve_memory_token_budget("planner", context_window=20_000) == 500


def test_resolve_context_window_known_model_and_safe_default():
    assert resolve_context_window(" gemini-2.5-flash-lite ") == MODEL_CONTEXT_WINDOWS[
        "gemini-2.5-flash-lite"
    ]
    assert resolve_context_window("future-model") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window(None) == DEFAULT_CONTEXT_WINDOW
