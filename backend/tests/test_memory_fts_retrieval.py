"""
Tests for Row 19 PR-C FTS rung-1 retrieval.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from backend.db import database
from backend.db.database import MEMORY_FTS_TABLE, engine
from backend.memory import memory_retriever, prompt_builder
from backend.memory.memory_fts import rebuild_memory_fts
from backend.memory.memory_retriever import (
    FTSMemoryRetriever,
    _fts_query_tokens,
)
from backend.memory.memory_store import add_fact, compute_content_hash
from backend.memory.prompt_builder import (
    ROLE_CATEGORIES,
    RequestContext,
    build_project_memory_block,
    build_project_memory_block_detailed,
)
from backend.pipeline import policy

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


def _require_fts5() -> None:
    if not _fts_available():
        pytest.skip("SQLite FTS5 is not available in this environment")


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


def _included_contents(result) -> list[str]:
    return [entry.content for entry in result.included_entries]


@pytest.mark.parametrize(
    ("role", "token_budget", "request_context"),
    [
        ("planner", 4000, RequestContext(title="uses")),
        ("triage", 400, RequestContext(files_expected=("backend/app.py",))),
        ("coder", 40, RequestContext(title="unrelated request")),
    ],
)
def test_flag_off_is_byte_identical_with_fts_populated(
    monkeypatch,
    memory_project_ids,
    role,
    token_budget,
    request_context,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Security fact: keep approvals visible.", category="security")
    add_fact(project_id, "Style fact uses Azure storage.", category="style")
    add_fact(project_id, "Stack fact: backend uses FastAPI.", category="stack")
    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", False)
    _drop_memory_fts_table()

    without_fts = build_project_memory_block_detailed(
        project_id,
        role=role,
        token_budget=token_budget,
        request_context=request_context,
    ).block
    with engine.begin() as conn:
        database._ensure_memory_fts_shape(conn)
    rebuild_memory_fts(project_id)
    with_fts = build_project_memory_block_detailed(
        project_id,
        role=role,
        token_budget=token_budget,
        request_context=request_context,
    ).block

    assert with_fts == without_fts


def test_fts_request_signal_reorders_relevance_but_not_candidate_set(
    monkeypatch,
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    baseline = add_fact(project_id, "Style fact alpha baseline.", category="style")
    fts_match = add_fact(project_id, "Style fact uses Azure storage.", category="style")
    rebuild_memory_fts(project_id)
    context = RequestContext(title="uses")

    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", False)
    rung0 = build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
    )
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)
    rung1 = build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
    )

    assert _included_contents(rung0) == [baseline["content"], fts_match["content"]]
    assert _included_contents(rung1) == [fts_match["content"], baseline["content"]]
    assert {entry.fact_id for entry in rung1.included_entries} == {
        baseline["id"],
        fts_match["id"],
    }


def test_fts_never_demotes_or_drops_mandatory_safety_candidates(
    monkeypatch,
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    security = add_fact(project_id, "Security fact: approvals stay visible.",
                        category="security")
    forbidden = add_fact(project_id, "Forbidden path fact: secrets stay out.",
                         category="forbidden_paths")
    style = add_fact(project_id, "Style fact uses Azure storage.", category="style")
    rebuild_memory_fts(project_id)
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)

    result = build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=RequestContext(title="uses"),
    )

    assert _included_contents(result)[:2] == [
        security["content"],
        forbidden["content"],
    ]
    assert style["content"] in _included_contents(result)
    assert {
        entry.category for entry in result.excluded_entries
    }.isdisjoint({"security", "forbidden_paths"})


def test_fts_project_scope_before_rank(monkeypatch, memory_project_ids):
    _require_fts5()
    project_a = _make_project_id(memory_project_ids, "project-a")
    project_b = _make_project_id(memory_project_ids, "project-b")
    fact_a = add_fact(project_a, "Style fact alpha baseline.", category="style")
    fact_b = add_fact(project_b, "Style fact uses Azure storage.", category="style")
    rebuild_memory_fts(project_a)
    rebuild_memory_fts(project_b)
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)

    candidates = FTSMemoryRetriever().retrieve_candidates(
        project_a,
        ROLE_CATEGORIES["planner"],
        request_context=RequestContext(title="uses"),
    )

    assert [row["id"] for row in candidates.in_policy_rows] == [fact_a["id"]]
    assert candidates.relevance_scores is None
    assert fact_b["id"] not in {
        row["id"]
        for row in candidates.in_policy_rows + candidates.out_of_policy_rows
    }


def test_fts_unavailable_degrades_to_rung_zero(
    monkeypatch,
    memory_project_ids,
):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Style fact alpha baseline.", category="style")
    add_fact(project_id, "Style fact uses Azure storage.", category="style")
    context = RequestContext(title="uses")
    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)

    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", False)
    rung0 = build_project_memory_block(project_id, role="planner")

    def fail_rank(*args, **kwargs):
        raise AssertionError("simulated FTS unavailable")

    monkeypatch.setattr(memory_retriever, "_rank_memory_fts_for_project", fail_rank)
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)
    degraded = build_project_memory_block_detailed(
        project_id,
        role="planner",
        request_context=context,
    ).block

    assert degraded == rung0


def test_missing_or_empty_fts_index_degrades_to_rung_zero(
    monkeypatch,
    memory_project_ids,
):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Style fact alpha baseline.", category="style")
    add_fact(project_id, "Style fact uses Azure storage.", category="style")
    context = RequestContext(title="uses")
    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)
    _drop_memory_fts_table()

    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", False)
    rung0 = build_project_memory_block(project_id, role="planner")
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)
    degraded = build_project_memory_block_detailed(
        project_id,
        role="planner",
        request_context=context,
    ).block

    assert degraded == rung0


def test_stale_fts_hash_mismatch_is_ignored(monkeypatch, memory_project_ids):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    baseline = add_fact(project_id, "Style fact alpha baseline.", category="style")
    stale_hit = add_fact(
        project_id,
        "Style fact uses legacy keyword.",
        category="style",
    )
    rebuild_memory_fts(project_id)
    updated_content = "Style fact neutral current text."
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts
            SET content = :content,
                content_hash = :content_hash
            WHERE id = :id
        """), {
            "id": stale_hit["id"],
            "content": updated_content,
            "content_hash": compute_content_hash(updated_content),
        })
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)

    candidates = FTSMemoryRetriever().retrieve_candidates(
        project_id,
        ROLE_CATEGORIES["planner"],
        request_context=RequestContext(title="uses"),
    )
    result = build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=RequestContext(title="uses"),
    )

    assert candidates.relevance_scores is None
    assert _included_contents(result) == [baseline["content"], updated_content]


def test_query_sanitization_drops_fts_syntax_and_caps_tokens(monkeypatch):
    monkeypatch.setattr(policy, "MEMORY_FTS_QUERY_TOKEN_CAP", 5)
    context = RequestContext(
        title='"alpha*" OR NEAR(beta gamma) content:secret foo-bar NOT and',
        description="delta:ignored epsilon zeta eta theta",
        files_expected=("frontend/src/App.tsx",),
    )

    tokens = _fts_query_tokens(context)

    assert tokens == ("alpha", "beta", "gamma", "foo", "bar")
    assert not {"or", "near", "not", "and", "content", "secret"} & set(tokens)


def test_query_sanitization_handles_unsafe_syntax_without_raising(
    monkeypatch,
    memory_project_ids,
):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Style fact alpha baseline.", category="style")
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)
    context = RequestContext(
        title='"alpha*" OR NEAR(beta gamma) content:secret foo-bar NOT and',
    )

    result = build_project_memory_block_detailed(
        project_id,
        role="planner",
        request_context=context,
    )

    assert result.included_count == 1
