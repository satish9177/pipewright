"""
Tests for Memory M1-B project memory prompt block builder.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.memory.memory_store import add_fact, archive_fact, load_hard_facts
from backend.memory.prompt_builder import build_project_memory_block

pytestmark = pytest.mark.unit


@pytest.fixture()
def memory_project_ids():
    project_ids = []
    yield project_ids
    with engine.begin() as conn:
        for project_id in project_ids:
            conn.execute(text("""
                DELETE FROM memory_facts
                WHERE project_id = :project_id
            """), {"project_id": project_id})


def make_project_id(memory_project_ids, label="project"):
    project_id = f"{label}-{uuid.uuid4().hex}"
    memory_project_ids.append(project_id)
    return project_id


def test_build_memory_block_empty_project_returns_empty_string(memory_project_ids):
    project_id = make_project_id(memory_project_ids)

    assert build_project_memory_block(project_id) == ""


def test_build_memory_block_requires_project_id(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "backend.memory.prompt_builder.logger.warning",
        lambda message, *args: warnings.append(message % args if args else message),
    )

    assert build_project_memory_block(None) == ""
    assert build_project_memory_block("  ") == ""
    assert any("without project_id" in warning for warning in warnings)


def test_build_memory_block_formats_header_and_footer(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Backend uses FastAPI", category="stack", scope="backend")

    block = build_project_memory_block(project_id, project_name="Demo")

    assert "=== PROJECT MEMORY (advisory; source code wins on conflict) ===" in block
    assert "Project: Demo" in block
    assert "source code / user instruction" in block
    assert "=== END PROJECT MEMORY ===" in block


def test_build_memory_block_includes_category_scope_prefix(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Backend uses FastAPI", category="stack", scope="backend")

    block = build_project_memory_block(project_id)

    assert "[stack/backend] Backend uses FastAPI" in block


def test_build_memory_block_excludes_archived_stale_historical(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    archived = add_fact(project_id, "Archived memory should not show", category="stack")
    stale = add_fact(project_id, "Stale memory should not show", category="stack")
    historical = add_fact(project_id, "Historical memory should not show", category="stack")
    add_fact(project_id, "Active memory should show", category="stack")
    archive_fact(project_id, archived["id"], "No longer true")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts
            SET status = 'stale', is_stale = 1
            WHERE id = :id
        """), {"id": stale["id"]})
        conn.execute(text("""
            UPDATE memory_facts
            SET status = 'historical'
            WHERE id = :id
        """), {"id": historical["id"]})

    block = build_project_memory_block(project_id)

    assert "Active memory should show" in block
    assert "Archived memory should not show" not in block
    assert "Stale memory should not show" not in block
    assert "Historical memory should not show" not in block


def test_build_memory_block_does_not_cross_project_boundaries(memory_project_ids):
    project_a = make_project_id(memory_project_ids, "project-a")
    project_b = make_project_id(memory_project_ids, "project-b")
    add_fact(project_a, "Project A uses FastAPI", category="stack")
    add_fact(project_b, "Project B uses Django", category="stack")

    block = build_project_memory_block(project_a)

    assert "Project A uses FastAPI" in block
    assert "Project B uses Django" not in block


def test_build_memory_block_respects_token_budget(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Security rule: never commit tokens", category="security")
    add_fact(project_id, "Structure rule: keep components under frontend pages", category="structure")

    block = build_project_memory_block(project_id, token_budget=16)

    assert "never commit tokens" in block
    assert "frontend pages" not in block
    assert "Budget used:" in block


def test_security_and_forbidden_paths_prioritized_under_budget(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Style rule: use concise labels", category="style", priority=0)
    add_fact(project_id, "Security rule: never expose API keys", category="security")
    add_fact(project_id, "Forbidden path rule: never modify .git", category="forbidden_paths")

    block = build_project_memory_block(project_id, token_budget=31)

    assert "never expose API keys" in block
    assert "concise labels" not in block


def test_triage_role_uses_small_budget_and_filters_categories(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Stack rule: backend uses FastAPI", category="stack")
    add_fact(project_id, "Reviewer preference: summarize risks", category="reviewer_pref")
    add_fact(project_id, "Style rule: use short labels", category="style")

    block = build_project_memory_block(project_id, role="triage")

    assert "Budget used:" in block
    assert "/ 400 tokens" in block
    assert "backend uses FastAPI" in block
    assert "Reviewer preference" not in block
    assert "short labels" not in block


def test_reviewer_role_includes_reviewer_pref(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Reviewer preference: mention rollback risk", category="reviewer_pref")

    block = build_project_memory_block(project_id, role="reviewer")

    assert "[reviewer_pref/global] Reviewer preference" in block


def test_coder_role_prefers_matching_scope(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Stack rule: frontend uses React", category="stack", scope="frontend")
    add_fact(project_id, "Stack rule: backend uses FastAPI", category="stack", scope="backend")
    add_fact(project_id, "Stack rule: global uses typed contracts", category="stack", scope="global")

    block = build_project_memory_block(project_id, role="coder", scopes=["backend"])

    global_index = block.index("global uses typed contracts")
    backend_index = block.index("backend uses FastAPI")
    frontend_index = block.index("frontend uses React")
    assert global_index < backend_index < frontend_index


def test_unknown_role_uses_safe_default(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Stack rule: backend uses FastAPI", category="stack")
    add_fact(project_id, "Reviewer preference: mention risk", category="reviewer_pref")

    block = build_project_memory_block(project_id, role="made-up-role")

    assert "backend uses FastAPI" in block
    assert "Reviewer preference" not in block


def test_prompt_builder_does_not_include_secrets_or_metadata(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(
        project_id,
        "Backend uses FastAPI",
        category="stack",
        source="manual-source",
        added_by="person-a",
        approved_by="person-b",
    )

    block = build_project_memory_block(project_id)

    assert "Backend uses FastAPI" in block
    assert "manual-source" not in block
    assert "person-a" not in block
    assert "person-b" not in block
    assert "archived_reason" not in block


def test_load_hard_facts_behavior_still_safe(memory_project_ids):
    project_a = make_project_id(memory_project_ids, "project-a")
    project_b = make_project_id(memory_project_ids, "project-b")
    add_fact(project_a, "Project A uses FastAPI", category="stack")
    add_fact(project_b, "Project B uses Django", category="stack")

    facts = load_hard_facts(project_a)

    assert "Project A uses FastAPI" in facts
    assert "Project B uses Django" not in facts


# ---------------------------------------------------------------------------
# #21F role-specific injection hardening: per-role category policy, advisory
# wrapper wording, deterministic budget truncation, and reviewer role support.
# ---------------------------------------------------------------------------

def _seed_all_category_facts(project_id):
    # One fact per real category so role filtering can be asserted precisely.
    add_fact(project_id, "Security fact: never expose API keys", category="security")
    add_fact(project_id, "Forbidden path fact: never modify .git", category="forbidden_paths")
    add_fact(project_id, "Stack fact: backend uses FastAPI", category="stack")
    add_fact(project_id, "Db fact: project uses SQLite", category="db")
    add_fact(project_id, "Test fact: run pytest backend tests", category="test")
    add_fact(project_id, "Structure fact: routes live under backend routes", category="structure")
    add_fact(project_id, "Architecture fact: patch applier owns writes", category="architecture")
    add_fact(project_id, "Style fact: prefer concise labels", category="style")
    add_fact(project_id, "Deploy fact: Docker compose for local dev", category="deploy")
    add_fact(project_id, "Reviewer pref fact: mention rollback risk", category="reviewer_pref")
    add_fact(project_id, "Other fact: rejected approach reuse helper", category="other")


def test_triage_role_sees_safety_but_not_style_or_reviewer_pref(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    _seed_all_category_facts(project_id)

    block = build_project_memory_block(project_id, role="triage")

    assert "never expose API keys" in block  # safety/security
    assert "never modify .git" in block       # safety/forbidden_paths
    assert "backend uses FastAPI" in block    # stack
    assert "prefer concise labels" not in block   # style excluded for triage
    assert "mention rollback risk" not in block   # reviewer_pref excluded
    assert "rejected approach reuse helper" not in block  # other excluded


def test_planner_sees_other_and_reviewer_pref(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    _seed_all_category_facts(project_id)

    block = build_project_memory_block(project_id, role="planner")

    assert "patch applier owns writes" in block      # architecture
    assert "rejected approach reuse helper" in block  # other (rejected-approach lessons)
    assert "mention rollback risk" in block           # reviewer_pref (user preference)


def test_coder_sees_other_for_patch_failure_lessons(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    _seed_all_category_facts(project_id)

    block = build_project_memory_block(project_id, role="coder")

    assert "backend uses FastAPI" in block            # stack
    assert "rejected approach reuse helper" in block   # other (patch-failure lessons)


def test_reviewer_role_is_focused_not_everything(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    _seed_all_category_facts(project_id)

    block = build_project_memory_block(project_id, role="reviewer")

    # Reviewer-relevant categories present.
    assert "never expose API keys" in block        # security
    assert "patch applier owns writes" in block    # architecture
    assert "mention rollback risk" in block        # reviewer_pref
    assert "rejected approach reuse helper" in block  # other
    # Narrowed out for reviewer.
    assert "backend uses FastAPI" not in block     # stack excluded
    assert "project uses SQLite" not in block      # db excluded
    assert "routes live under backend routes" not in block  # structure excluded


def test_every_role_block_has_advisory_override_wrapper(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Security fact: never expose API keys", category="security")

    for role in ("triage", "planner", "coder", "reviewer"):
        block = build_project_memory_block(project_id, role=role)
        assert "Memory is advisory context only." in block
        # Memory must never override source code / request / tests / safety rules.
        assert "source code / user instruction / tests / safety rules" in block


def test_budget_truncation_is_deterministic_and_keeps_safety(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Security rule: never expose API keys", category="security", priority=10)
    for index in range(12):
        add_fact(
            project_id,
            f"Style rule number {index}: keep helpers small and tidy here",
            category="style",
            priority=500,
        )

    first = build_project_memory_block(project_id, role="planner", token_budget=40)
    second = build_project_memory_block(project_id, role="planner", token_budget=40)

    def memory_lines(block):
        return [line for line in block.splitlines() if line.startswith("[")]

    # Deterministic: identical selected entries and order across calls (the only
    # varying header field is the Generated timestamp, which is not memory).
    assert memory_lines(first) == memory_lines(second)
    assert len(memory_lines(first)) >= 1
    # Safety/security is ranked first and survives truncation.
    assert memory_lines(first)[0].startswith("[security/")
    assert "never expose API keys" in first
    # Budget actually truncated some style facts.
    assert "Style rule number 11" not in first


def test_role_block_fails_closed_without_project_id():
    assert build_project_memory_block(None, role="planner") == ""
    assert build_project_memory_block("   ", role="reviewer") == ""


def test_role_block_excludes_stale_and_archived(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    active = add_fact(project_id, "Active security rule stays", category="security")
    archived = add_fact(project_id, "Archived security rule goes", category="security")
    stale = add_fact(project_id, "Stale security rule goes", category="security")
    archive_fact(project_id, archived["id"], "No longer true")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts SET status = 'stale', is_stale = 1 WHERE id = :id
        """), {"id": stale["id"]})

    block = build_project_memory_block(project_id, role="reviewer")

    assert active["content"] in block
    assert "Archived security rule goes" not in block
    assert "Stale security rule goes" not in block


def test_role_block_is_project_scoped(memory_project_ids):
    project_a = make_project_id(memory_project_ids, "project-a")
    project_b = make_project_id(memory_project_ids, "project-b")
    add_fact(project_a, "Project A security rule", category="security")
    add_fact(project_b, "Project B security rule", category="security")

    block = build_project_memory_block(project_a, role="reviewer")

    assert "Project A security rule" in block
    assert "Project B security rule" not in block


def test_build_memory_block_excludes_unscoped_legacy_rows(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Scoped backend fact", category="stack")
    legacy_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO memory_facts
            (id, content, category, scope, priority, source, added_by,
             status, is_stale)
            VALUES (:id, 'Unscoped legacy memory must not leak', 'stack',
                    'global', 100, 'legacy', 'test', 'active', 0)
        """), {"id": legacy_id})

    try:
        block = build_project_memory_block(project_id)

        assert "Scoped backend fact" in block
        assert "Unscoped legacy memory must not leak" not in block
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM memory_facts WHERE id = :id"),
                {"id": legacy_id},
            )
