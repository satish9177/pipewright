"""
Tests for Row 19 PR-B's byte-identical MemoryRetriever seam.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from backend.db import database
from backend.db.database import MEMORY_FTS_TABLE, engine
from backend.memory import memory_fts, memory_retriever, prompt_builder
from backend.memory.memory_fts import rebuild_memory_fts
from backend.memory.memory_retriever import (
    DeterministicMemoryRetriever,
    RetrievedCandidates,
    default_memory_retriever,
)
from backend.memory.memory_store import (
    add_fact,
    archive_fact,
    compute_content_hash,
    mark_fact_stale,
)
from backend.memory.prompt_builder import (
    ROLE_CATEGORIES,
    RequestContext,
    build_project_memory_block,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def memory_project_ids():
    project_ids = []
    yield project_ids
    with engine.begin() as conn:
        for project_id in project_ids:
            if _table_exists(conn, MEMORY_FTS_TABLE):
                conn.execute(
                    text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE project_id = :p"),
                    {"p": project_id},
                )
            conn.execute(
                text("DELETE FROM memory_suggestions WHERE project_id = :p"),
                {"p": project_id},
            )
            conn.execute(
                text("DELETE FROM memory_facts WHERE project_id = :p"),
                {"p": project_id},
            )


def _make_project_id(memory_project_ids, label="project") -> str:
    project_id = f"{label}-{uuid.uuid4().hex}"
    memory_project_ids.append(project_id)
    return project_id


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(text("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = :table_name
    """), {"table_name": table_name}).fetchone()
    return row is not None


def _drop_memory_fts_table() -> None:
    with engine.begin() as conn:
        if _table_exists(conn, MEMORY_FTS_TABLE):
            conn.execute(text(f"DROP TABLE {MEMORY_FTS_TABLE}"))


def _fts_available() -> bool:
    with engine.connect() as conn:
        return database._sqlite_fts5_available(conn)


def _legacy_candidate_load(
    project_id: str,
    categories: set[str],
) -> tuple[list[dict], list[dict]]:
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

    in_policy: list[dict] = []
    out_of_policy: list[dict] = []
    for row in rows:
        if (row.get("category") or "other") in categories:
            in_policy.append(row)
        else:
            out_of_policy.append(row)
    return in_policy, out_of_policy


def _insert_pending_suggestion(project_id: str, content: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO memory_suggestions (
                id,
                project_id,
                content,
                category,
                scope,
                priority,
                source,
                evidence_excerpt,
                status,
                content_hash,
                rationale
            )
            VALUES (
                :id,
                :project_id,
                :content,
                'stack',
                'backend',
                100,
                'bootstrap',
                'suggestion evidence must not be returned',
                'pending',
                :content_hash,
                'suggestion rationale must not be returned'
            )
        """), {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "content": content,
            "content_hash": compute_content_hash(content),
        })


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


@pytest.fixture(params=[
    pytest.param(default_memory_retriever, id="default-deterministic"),
])
def retriever_factory(request):
    return request.param


def test_deterministic_retriever_matches_legacy_candidate_load(memory_project_ids):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Security fact: keep approval gates", category="security")
    add_fact(project_id, "Stack fact: backend uses FastAPI", category="stack")
    add_fact(project_id, "Reviewer preference: mention risk", category="reviewer_pref")
    categories = ROLE_CATEGORIES["triage"]

    expected_in_policy, expected_out_of_policy = _legacy_candidate_load(
        project_id,
        categories,
    )
    candidates = DeterministicMemoryRetriever().retrieve_candidates(
        project_id,
        categories,
        request_context=None,
    )

    assert candidates.in_policy_rows == expected_in_policy
    assert candidates.out_of_policy_rows == expected_out_of_policy


def test_retriever_project_scope_isolation(memory_project_ids, retriever_factory):
    project_a = _make_project_id(memory_project_ids, "project-a")
    project_b = _make_project_id(memory_project_ids, "project-b")
    fact_a = add_fact(project_a, "Project A scoped memory", category="stack")
    fact_b = add_fact(project_b, "Project B scoped memory", category="stack")

    candidates = retriever_factory().retrieve_candidates(
        project_a,
        ROLE_CATEGORIES["planner"],
        request_context=None,
    )
    returned_ids = {row["id"] for row in candidates.in_policy_rows}

    assert fact_a["id"] in returned_ids
    assert fact_b["id"] not in returned_ids


def test_retriever_excludes_stale_archived_and_historical(
    memory_project_ids,
    retriever_factory,
):
    project_id = _make_project_id(memory_project_ids)
    active = add_fact(project_id, "Active fact stays visible", category="stack")
    archived = add_fact(project_id, "Archived fact must not return", category="stack")
    stale = add_fact(project_id, "Stale fact must not return", category="stack")
    historical = add_fact(
        project_id,
        "Historical fact must not return",
        category="stack",
    )
    archive_fact(project_id, archived["id"], "No longer true")
    mark_fact_stale(project_id, stale["id"], "No longer current")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts
            SET status = 'historical'
            WHERE id = :id
        """), {"id": historical["id"]})

    candidates = retriever_factory().retrieve_candidates(
        project_id,
        ROLE_CATEGORIES["planner"],
        request_context=None,
    )
    returned_ids = {row["id"] for row in candidates.in_policy_rows}

    assert returned_ids == {active["id"]}


def test_retriever_never_returns_suggestions(memory_project_ids, retriever_factory):
    project_id = _make_project_id(memory_project_ids)
    fact = add_fact(project_id, "Canonical approved memory", category="stack")
    _insert_pending_suggestion(project_id, "Pending suggestion must not return")

    candidates = retriever_factory().retrieve_candidates(
        project_id,
        ROLE_CATEGORIES["planner"],
        request_context=None,
    )
    returned_content = "\n".join(
        row["content"]
        for rows in (candidates.in_policy_rows, candidates.out_of_policy_rows)
        for row in rows
    )

    assert fact["content"] in returned_content
    assert "Pending suggestion" not in returned_content
    assert "suggestion evidence" not in returned_content
    assert "suggestion rationale" not in returned_content


@pytest.mark.parametrize("project_id", ["", "   "])
def test_retriever_blank_project_id_returns_empty_candidates(
    project_id,
    retriever_factory,
):
    candidates = retriever_factory().retrieve_candidates(
        project_id,
        ROLE_CATEGORIES["planner"],
        request_context=None,
    )

    assert candidates == RetrievedCandidates(
        in_policy_rows=[],
        out_of_policy_rows=[],
    )


def test_retriever_does_not_cap_or_drop_mandatory_candidates(
    memory_project_ids,
    retriever_factory,
):
    project_id = _make_project_id(memory_project_ids)
    safety = add_fact(
        project_id,
        "Security fact: approval gates stay mandatory",
        category="security",
    )
    forbidden = add_fact(
        project_id,
        "Forbidden path fact: generated secrets stay out of memory",
        category="forbidden_paths",
    )
    style_facts = [
        add_fact(project_id, f"Style fact {index}: keep labels concise", category="style")
        for index in range(20)
    ]

    candidates = retriever_factory().retrieve_candidates(
        project_id,
        {"security", "forbidden_paths", "style"},
        request_context=None,
    )
    returned_ids = {row["id"] for row in candidates.in_policy_rows}

    assert safety["id"] in returned_ids
    assert forbidden["id"] in returned_ids
    assert {fact["id"] for fact in style_facts}.issubset(returned_ids)
    assert len(candidates.in_policy_rows) == 22


def test_rung_zero_ignores_request_context(memory_project_ids, retriever_factory):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Backend fact mentions FastAPI", category="stack")
    add_fact(project_id, "Frontend fact mentions React", category="stack")
    context = RequestContext(
        title="React UI change",
        description="Touch frontend screens",
        files_expected=("frontend/src/App.tsx",),
        steer_text="Prefer the React memory",
    )

    without_context = retriever_factory().retrieve_candidates(
        project_id,
        ROLE_CATEGORIES["planner"],
        request_context=None,
    )
    with_context = retriever_factory().retrieve_candidates(
        project_id,
        ROLE_CATEGORIES["planner"],
        request_context=context,
    )

    assert with_context == without_context


def test_retriever_module_does_not_import_or_read_fts():
    source = inspect.getsource(memory_retriever)

    assert "memory_fts" not in source
    assert "memory_facts_fts" not in source
    assert "MATCH" not in source


def test_prompt_block_is_byte_identical_to_legacy_candidate_load(
    monkeypatch,
    memory_project_ids,
):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Security fact: keep scope guard", category="security")
    add_fact(project_id, "Stack fact: backend uses FastAPI", category="stack")
    add_fact(project_id, "Style fact: keep labels concise", category="style")
    context = RequestContext(
        title="FastAPI backend change",
        files_expected=("backend/routes/memory.py",),
    )

    class LegacyCandidateRetriever:
        def retrieve_candidates(self, project_id, categories, request_context=None):
            del request_context
            in_policy, out_of_policy = _legacy_candidate_load(project_id, categories)
            return RetrievedCandidates(in_policy, out_of_policy)

    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        prompt_builder,
        "default_memory_retriever",
        lambda: LegacyCandidateRetriever(),
    )
    legacy_block = prompt_builder.build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=200,
        request_context=context,
    ).block

    monkeypatch.setattr(
        prompt_builder,
        "default_memory_retriever",
        memory_retriever.default_memory_retriever,
    )
    seam_block = prompt_builder.build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=200,
        request_context=context,
    ).block

    assert seam_block == legacy_block


def test_fts_table_presence_does_not_change_prompt_memory_block(
    monkeypatch,
    memory_project_ids,
):
    if not _fts_available():
        pytest.skip("SQLite FTS5 is not available in this environment")

    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "FTS dormant stack fact for backend memory", category="stack")
    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)
    _drop_memory_fts_table()

    without_fts = build_project_memory_block(project_id, role="planner")
    with engine.begin() as conn:
        database._ensure_memory_fts_shape(conn)
    rebuild_memory_fts(project_id)

    def fail_on_match(*args, **kwargs):
        raise AssertionError("PR-B must not read FTS")

    monkeypatch.setattr(memory_fts, "_match_memory_fts_for_project", fail_on_match)
    with_fts = build_project_memory_block(project_id, role="planner")

    assert with_fts == without_fts
