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
    active = add_fact(project_id, "Active memory should show", category="stack")
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

    block = build_project_memory_block(project_id, token_budget=18)

    assert "never expose API keys" in block
    assert "concise labels" not in block


def test_triage_role_uses_small_budget_and_filters_categories(memory_project_ids):
    project_id = make_project_id(memory_project_ids)
    add_fact(project_id, "Stack rule: backend uses FastAPI", category="stack")
    add_fact(project_id, "Reviewer preference: summarize risks", category="reviewer_pref")
    add_fact(project_id, "Style rule: use short labels", category="style")

    block = build_project_memory_block(project_id, role="triage")

    assert "Budget used:" in block
    assert "/ 300 tokens" in block
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
