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

    with pytest.raises(MandatoryMemoryBudgetExceeded, match="mandatory tier"):
        build_project_memory_block_detailed(
            project_id=project_id,
            role="planner",
            token_budget=mandatory_budget - 1,
        )


def test_not_relevant_reason_not_emitted_when_flag_off(project_ids):
    # With MEMORY_RELEVANCE_OMISSION_ENABLED at its shipped default (False), the
    # reason stays defined-but-dormant even under a binding budget.
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


# --- Row 12 PR-C: request-aware relevance omission + priority-based pinning ----
#
# The flag MEMORY_RELEVANCE_OMISSION_ENABLED ships False; every test below that
# exercises new behavior enables it explicitly via monkeypatch. The flag-off
# default is the PR-B parity guard.

# A request that strongly matches the "frontend App tsx" signal facts and not the
# "database migration" zero-signal facts.
_REL_CONTEXT = RequestContext(
    title="Update the frontend App view",
    files_expected=("frontend/src/App.tsx",),
)


def _enable_omission(monkeypatch):
    monkeypatch.setattr(policy, "MEMORY_RELEVANCE_OMISSION_ENABLED", True)


def _seed_mixed_relevance(project_ids, zero_count):
    """One signal-bearing relevance fact + ``zero_count`` zero-signal ones."""
    project_id = _new_project(project_ids)
    signal = add_fact(
        project_id,
        "Frontend App tsx rendering stays simple.",
        category="style",
        priority=100,
    )
    zeros = [
        add_fact(
            project_id,
            f"Database migration note number {index} stays small.",
            category="style",
            priority=100,
        )
        for index in range(zero_count)
    ]
    return project_id, signal, zeros


def test_omission_disabled_by_default_above_grace(project_ids):
    # Flag at its shipped default (False): even a 14-fact mixed-signal store with
    # budget to spare injects everything — PR-B parity, no not_relevant.
    project_id, signal, zeros = _seed_mixed_relevance(project_ids, zero_count=13)

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=_REL_CONTEXT,
    )

    assert len(result.included_entries) == 14
    assert {entry.content for entry in result.included_entries} == (
        {signal["content"]} | {zero["content"] for zero in zeros}
    )
    assert EXCLUSION_NOT_RELEVANT_TO_REQUEST not in {
        entry.exclusion_reason for entry in result.excluded_entries
    }


def test_omission_emits_not_relevant_above_grace_when_flag_on(
    project_ids, monkeypatch
):
    _enable_omission(monkeypatch)
    project_id, signal, zeros = _seed_mixed_relevance(project_ids, zero_count=13)

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,  # budget to spare: omission, not budget, drops them
        request_context=_REL_CONTEXT,
    )

    assert [entry.content for entry in result.included_entries] == [signal["content"]]
    assert {entry.exclusion_reason for entry in result.excluded_entries} == {
        EXCLUSION_NOT_RELEVANT_TO_REQUEST
    }
    assert {entry.content for entry in result.excluded_entries} == {
        zero["content"] for zero in zeros
    }


def test_safety_facts_never_omitted_as_not_relevant_when_flag_on(
    project_ids,
    monkeypatch,
):
    _enable_omission(monkeypatch)
    project_id = _new_project(project_ids)
    security = add_fact(
        project_id,
        "Never leak tokens.",
        category="security",
        priority=100,
    )
    forbidden = add_fact(
        project_id,
        "Keep secret files untouched.",
        category="forbidden_paths",
        priority=100,
    )
    signal = add_fact(
        project_id,
        "Frontend App tsx rendering stays simple.",
        category="style",
        priority=100,
    )
    zeros = [
        add_fact(
            project_id,
            f"Database migration note number {index} stays small.",
            category="style",
            priority=100,
        )
        for index in range(13)
    ]

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=_REL_CONTEXT,
    )

    included = {
        (entry.content, entry.category, entry.exclusion_reason)
        for entry in result.included_entries
    }
    assert (security["content"], "security", None) in included
    assert (forbidden["content"], "forbidden_paths", None) in included
    assert (signal["content"], "style", None) in included
    assert all(
        entry.category not in {"security", "forbidden_paths"}
        for entry in result.excluded_entries
    )
    assert {
        entry.content
        for entry in result.excluded_entries
        if entry.exclusion_reason == EXCLUSION_NOT_RELEVANT_TO_REQUEST
    } == {zero["content"] for zero in zeros}


def test_omission_no_op_at_grace_threshold_when_flag_on(project_ids, monkeypatch):
    # Exactly 12 relevance candidates: at the threshold, not above it, so nothing
    # is omitted even with the flag on.
    _enable_omission(monkeypatch)
    project_id, signal, zeros = _seed_mixed_relevance(project_ids, zero_count=11)

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=_REL_CONTEXT,
    )

    assert len(result.included_entries) == 12
    assert EXCLUSION_NOT_RELEVANT_TO_REQUEST not in {
        entry.exclusion_reason for entry in result.excluded_entries
    }


def test_all_zero_signal_omits_nothing_when_flag_on(project_ids, monkeypatch):
    # 13 relevance facts, all zero-signal, with a request that DOES carry signal
    # fields (so scoring runs) but matches none of them. The all-zero degeneracy
    # guard must inject everything rather than omit the whole tier.
    _enable_omission(monkeypatch)
    project_id = _new_project(project_ids)
    zeros = [
        add_fact(
            project_id,
            f"Database migration note number {index} stays small.",
            category="style",
            priority=100,
        )
        for index in range(13)
    ]

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=_REL_CONTEXT,
    )

    assert {entry.content for entry in result.included_entries} == {
        zero["content"] for zero in zeros
    }
    assert result.excluded_entries == ()


def test_omission_and_budget_drop_coexist_with_deterministic_order(
    project_ids, monkeypatch
):
    _enable_omission(monkeypatch)
    project_id = _new_project(project_ids)
    signal_high = add_fact(
        project_id,
        "Frontend App tsx rendering stays simple.",  # path overlap 3
        category="style",
        priority=100,
    )
    add_fact(
        project_id,
        "Frontend view tweak only.",  # weaker signal (path overlap 1)
        category="style",
        priority=100,
    )
    for index in range(12):
        add_fact(
            project_id,
            f"Database migration note number {index} stays small.",
            category="style",
            priority=100,
        )

    # Budget fits exactly the single highest-signal line: the weaker signal fact is
    # budget_dropped, the 12 zero-signal facts are omitted as not_relevant.
    budget = _line_tokens("style", "global", signal_high["content"])
    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=budget,
        request_context=_REL_CONTEXT,
    )

    assert [entry.content for entry in result.included_entries] == [
        signal_high["content"]
    ]
    reasons = [entry.exclusion_reason for entry in result.excluded_entries]
    # Budget drops are surfaced before relevance omissions (display ordering).
    assert reasons[0] == EXCLUSION_BUDGET_DROPPED
    assert set(reasons[1:]) == {EXCLUSION_NOT_RELEVANT_TO_REQUEST}
    assert reasons.count(EXCLUSION_NOT_RELEVANT_TO_REQUEST) == 12


def test_priority_pinned_fact_joins_mandatory_tier_when_flag_on(
    project_ids, monkeypatch
):
    # Pinning is request_context-independent: it applies even with no request.
    _enable_omission(monkeypatch)
    project_id = _new_project(project_ids)
    pinned = add_fact(
        project_id,
        "Pinned style guidance always applies.",
        category="style",
        priority=5,
    )
    normal = add_fact(
        project_id,
        "Normal style note here.",
        category="style",
        priority=100,
    )

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=None,
    )

    # Pinned fact renders first (mandatory tier) and is never excluded.
    assert result.included_entries[0].content == pinned["content"]
    assert normal["content"] in {
        entry.content for entry in result.included_entries
    }
    assert result.excluded_entries == ()


def test_pinned_fact_is_never_budget_dropped_when_flag_on(project_ids, monkeypatch):
    _enable_omission(monkeypatch)
    project_id = _new_project(project_ids)
    pinned = add_fact(
        project_id,
        "Pinned rule wins room.",
        category="style",
        priority=5,
    )
    normal = add_fact(
        project_id,
        "Ordinary relevance note that should be dropped.",
        category="style",
        priority=100,
    )
    # Budget fits only the pinned line.
    budget = _line_tokens("style", "global", pinned["content"])

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=budget,
        request_context=None,
    )

    assert [entry.content for entry in result.included_entries] == [pinned["content"]]
    assert [
        (entry.content, entry.exclusion_reason)
        for entry in result.excluded_entries
    ] == [(normal["content"], EXCLUSION_BUDGET_DROPPED)]


def test_pinned_overflow_raises_mandatory_exceeded_when_flag_on(
    project_ids, monkeypatch
):
    _enable_omission(monkeypatch)
    project_id = _new_project(project_ids)
    pinned = add_fact(
        project_id,
        "Pinned rule that alone exceeds the budget here.",
        category="style",
        priority=5,
    )
    budget = _line_tokens("style", "global", pinned["content"]) - 1

    with pytest.raises(MandatoryMemoryBudgetExceeded, match="mandatory tier"):
        build_project_memory_block_detailed(
            project_id=project_id,
            role="planner",
            token_budget=budget,
            request_context=None,
        )


def test_low_priority_fact_is_not_pinned_when_flag_off(project_ids):
    # Flag off (default): a priority<=10 fact is an ordinary, droppable relevance
    # fact — NOT mandatory — so an over-budget store drops it instead of raising.
    project_id = _new_project(project_ids)
    low = add_fact(
        project_id,
        "Low priority but not pinned while flag off.",
        category="style",
        priority=5,
    )
    budget = _line_tokens("style", "global", low["content"]) - 1

    result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=budget,
        request_context=None,
    )

    assert result.included_entries == ()
    assert [
        (entry.content, entry.exclusion_reason)
        for entry in result.excluded_entries
    ] == [(low["content"], EXCLUSION_BUDGET_DROPPED)]


def test_omission_is_deterministic_when_flag_on(project_ids, monkeypatch):
    _enable_omission(monkeypatch)
    project_id, _signal, _zeros = _seed_mixed_relevance(project_ids, zero_count=13)

    first = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=_REL_CONTEXT,
    )
    second = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        token_budget=4000,
        request_context=_REL_CONTEXT,
    )

    assert first.included_entries == second.included_entries
    assert first.excluded_entries == second.excluded_entries
