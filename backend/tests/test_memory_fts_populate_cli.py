"""
Tests for Row 19 FTS populate PR-1/PR-2 explicit CLI tooling.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
import re

import pytest
from sqlalchemy import text

from backend.db import database
from backend.db.database import MEMORY_FTS_TABLE, engine
from backend.memory import prompt_builder
from backend.memory.memory_store import (
    add_fact,
    archive_fact,
    compute_content_hash,
    mark_fact_stale,
)
from backend.memory.prompt_builder import RequestContext
from backend.pipeline import policy
from scripts import rebuild_memory_fts as rebuild_cli

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


def _fts_available() -> bool:
    with engine.connect() as conn:
        return database._sqlite_fts5_available(conn)


def _require_fts5() -> None:
    if not _fts_available():
        pytest.skip("SQLite FTS5 is not available in this environment")


def _fts_rows(project_id: str) -> list[dict]:
    with engine.connect() as conn:
        if not _table_exists(conn, MEMORY_FTS_TABLE):
            return []
        rows = conn.execute(text(f"""
            SELECT fact_id, project_id, category, scope, content_hash, content
            FROM {MEMORY_FTS_TABLE}
            WHERE project_id = :project_id
            ORDER BY fact_id
        """), {"project_id": project_id}).fetchall()
    return [dict(row._mapping) for row in rows]


def _memory_fact_rows(project_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, project_id, content, category, scope, status, is_stale,
                   content_hash
            FROM memory_facts
            WHERE project_id = :project_id
            ORDER BY id
        """), {"project_id": project_id}).fetchall()
    return [dict(row._mapping) for row in rows]


def _memory_suggestion_rows(project_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, project_id, content, status, content_hash
            FROM memory_suggestions
            WHERE project_id = :project_id
            ORDER BY id
        """), {"project_id": project_id}).fetchall()
    return [dict(row._mapping) for row in rows]


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
                evidence_path,
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
                'backend/example.py',
                'suggestion evidence must not be indexed',
                'pending',
                :content_hash,
                'suggestion rationale must not be indexed'
            )
        """), {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "content": content,
            "content_hash": compute_content_hash(content),
        })


class NonInteractiveStdin:
    def isatty(self) -> bool:
        return False


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


def test_rebuild_requires_project_id():
    with pytest.raises(SystemExit) as error:
        rebuild_cli.parse_args(["rebuild", "--yes"])

    assert error.value.code == 2


def test_rebuild_refuses_noninteractive_without_yes(monkeypatch, capsys):
    monkeypatch.setattr(rebuild_cli.sys, "stdin", NonInteractiveStdin())
    monkeypatch.setattr(rebuild_cli.database, "init_db", lambda: None)
    monkeypatch.setattr(rebuild_cli, "_fts5_available", lambda: True)
    monkeypatch.setattr(rebuild_cli, "rebuild_memory_fts", lambda project_id: 0)

    code = rebuild_cli.main(["rebuild", "--project-id", "project-no-yes"])

    output = capsys.readouterr().out
    assert code == 2
    assert "without --yes" in output


def test_rebuild_reports_fts5_unavailable_nonfatally(monkeypatch, capsys):
    monkeypatch.setattr(rebuild_cli.database, "init_db", lambda: None)
    monkeypatch.setattr(rebuild_cli, "_fts5_available", lambda: False)

    def fail_rebuild(project_id):
        raise AssertionError("rebuild must not run when FTS5 is unavailable")

    monkeypatch.setattr(rebuild_cli, "rebuild_memory_fts", fail_rebuild)

    code = rebuild_cli.main(["rebuild", "--project-id", "project-no-fts"])

    output = capsys.readouterr().out
    assert code == 0
    assert "FTS5 unavailable" in output
    assert "project_id=project-no-fts" in output
    assert "rebuilt_rows=0" in output


def test_rebuild_cli_populates_only_active_scoped_non_stale_facts(
    capsys,
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids, "project-a")
    other_project_id = _make_project_id(memory_project_ids, "project-b")
    active = add_fact(
        project_id,
        "Alpha active memory fact for backend search",
        category="stack",
        scope="backend",
    )
    archived = add_fact(project_id, "Alpha archived memory must not index")
    stale = add_fact(project_id, "Alpha stale memory must not index")
    historical = add_fact(project_id, "Alpha historical memory must not index")
    add_fact(other_project_id, "Alpha cross project memory must not index")
    _insert_pending_suggestion(
        project_id,
        "Alpha pending suggestion content must not index",
    )

    archive_fact(project_id, archived["id"], "No longer true")
    mark_fact_stale(project_id, stale["id"], "No longer current")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE memory_facts
            SET status = 'historical', is_stale = 1
            WHERE id = :id
        """), {"id": historical["id"]})

    code = rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"])

    output = capsys.readouterr().out
    assert code == 0
    assert f"project_id={project_id}" in output
    assert "rebuilt_rows=1" in output

    rows = _fts_rows(project_id)
    assert [row["fact_id"] for row in rows] == [active["id"]]
    assert rows[0]["content"] == active["content"]
    assert rows[0]["project_id"] == project_id
    assert rows[0]["category"] == "stack"
    assert rows[0]["scope"] == "backend"
    assert rows[0]["content_hash"] == active["content_hash"]
    indexed_text = "\n".join(row["content"] for row in rows)
    for excluded in (
        "archived memory",
        "stale memory",
        "historical memory",
        "cross project memory",
        "pending suggestion",
        "suggestion evidence",
        "suggestion rationale",
    ):
        assert excluded not in indexed_text


def test_rebuild_project_a_does_not_change_project_b_rows(
    memory_project_ids,
):
    _require_fts5()
    project_a = _make_project_id(memory_project_ids, "project-a")
    project_b = _make_project_id(memory_project_ids, "project-b")
    fact_a = add_fact(project_a, "Bravo project A FTS memory", category="stack")
    add_fact(project_b, "Bravo project B FTS memory", category="stack")
    assert rebuild_cli.main(["rebuild", "--project-id", project_b, "--yes"]) == 0
    project_b_rows = _fts_rows(project_b)

    assert rebuild_cli.main(["rebuild", "--project-id", project_a, "--yes"]) == 0

    assert _fts_rows(project_b) == project_b_rows
    assert [row["fact_id"] for row in _fts_rows(project_a)] == [fact_a["id"]]


def test_memory_store_mutations_do_not_update_fts_until_explicit_rebuild(
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    stale_after_mutation = add_fact(
        project_id,
        "Charlie fact initially indexed",
        category="stack",
    )
    assert rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"]) == 0
    before_mutations = _fts_rows(project_id)

    fresh_after_mutation = add_fact(
        project_id,
        "Charlie fact added after rebuild",
        category="stack",
    )
    mark_fact_stale(
        project_id,
        stale_after_mutation["id"],
        "No longer current",
    )

    assert _fts_rows(project_id) == before_mutations

    assert rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"]) == 0
    assert [row["fact_id"] for row in _fts_rows(project_id)] == [
        fresh_after_mutation["id"],
    ]


@pytest.mark.parametrize("index_state", ["empty", "stale"])
def test_flag_on_read_does_not_populate_and_degrades_safely(
    monkeypatch,
    memory_project_ids,
    index_state,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    fact = add_fact(project_id, "Delta fact uses legacy token", category="style")
    monkeypatch.setattr(prompt_builder, "datetime", FrozenDateTime)
    context = RequestContext(title="uses")

    if index_state == "empty":
        with engine.begin() as conn:
            database._ensure_memory_fts_shape(conn)
            conn.execute(
                text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE project_id = :p"),
                {"p": project_id},
            )
    else:
        assert rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"]) == 0
        updated_content = "Delta fact current text without signal"
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE memory_facts
                SET content = :content,
                    content_hash = :content_hash
                WHERE id = :id
            """), {
                "id": fact["id"],
                "content": updated_content,
                "content_hash": compute_content_hash(updated_content),
            })

    rows_before_read = _fts_rows(project_id)

    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", False)
    rung0 = prompt_builder.build_project_memory_block_detailed(
        project_id,
        role="planner",
        request_context=context,
    ).block
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", True)
    flag_on = prompt_builder.build_project_memory_block_detailed(
        project_id,
        role="planner",
        request_context=context,
    ).block

    assert flag_on == rung0
    assert _fts_rows(project_id) == rows_before_read


def test_compare_real_project_is_read_only_and_reports_invariants(
    monkeypatch,
    capsys,
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    add_fact(
        project_id,
        "Security fact: approval gates stay mandatory.",
        category="security",
    )
    add_fact(project_id, "Style fact alpha baseline.", category="style")
    add_fact(project_id, "Style fact uses Azure storage.", category="style")
    assert rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"]) == 0
    fact_rows_before = _memory_fact_rows(project_id)
    suggestion_rows_before = _memory_suggestion_rows(project_id)
    fts_rows_before = _fts_rows(project_id)

    def fail_init():
        raise AssertionError("real-project compare must not run init_db")

    def fail_rebuild(project_id):
        raise AssertionError("real-project compare must not rebuild FTS")

    monkeypatch.setattr(rebuild_cli.database, "init_db", fail_init)
    monkeypatch.setattr(rebuild_cli, "rebuild_memory_fts", fail_rebuild)

    code = rebuild_cli.main([
        "compare",
        "--project-id",
        project_id,
        "--role",
        "planner",
        "--title",
        "uses",
        "--token-budget",
        "4000",
    ])

    output = capsys.readouterr().out
    assert code == 0
    assert "included_set_identical=True" in output
    assert "mandatory_tier_identical=True" in output
    assert "only_relevance_tier_may_reorder=True" in output
    assert "fts_coverage_count=1" in output
    assert "fallback_count=0" in output
    assert "no_cross_project_facts=True" in output
    assert "deterministic_output=True" in output
    assert _memory_fact_rows(project_id) == fact_rows_before
    assert _memory_suggestion_rows(project_id) == suggestion_rows_before
    assert _fts_rows(project_id) == fts_rows_before


def test_compare_structured_entries_keep_set_and_mandatory_tier_identical(
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    security = add_fact(
        project_id,
        "Security fact: approval gates stay mandatory.",
        category="security",
    )
    baseline = add_fact(project_id, "Style fact alpha baseline.", category="style")
    fts_match = add_fact(
        project_id,
        "Style fact uses Azure storage.",
        category="style",
    )
    assert rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"]) == 0
    context = RequestContext(title="uses")

    off_entries = rebuild_cli._included_entries(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
        fts_enabled=False,
    )
    on_entries = rebuild_cli._included_entries(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
        fts_enabled=True,
    )
    report = rebuild_cli.compare_memory_fts(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
    )

    assert {entry.fact_id for entry in off_entries} == {
        entry.fact_id for entry in on_entries
    } == {security["id"], baseline["id"], fts_match["id"]}
    assert rebuild_cli._mandatory_tier(off_entries) == rebuild_cli._mandatory_tier(
        on_entries
    )
    assert [entry.fact_id for entry in rebuild_cli._mandatory_tier(on_entries)] == [
        security["id"],
    ]
    assert {
        entry.fact_id for entry in rebuild_cli._relevance_entries(off_entries)
    } == {
        entry.fact_id for entry in rebuild_cli._relevance_entries(on_entries)
    } == {baseline["id"], fts_match["id"]}
    assert [
        entry.fact_id for entry in rebuild_cli._relevance_entries(off_entries)
    ] != [
        entry.fact_id for entry in rebuild_cli._relevance_entries(on_entries)
    ]
    assert report.included_set_identical is True
    assert report.mandatory_tier_identical is True
    assert report.only_relevance_tier_may_reorder is True
    assert report.relevance_order_delta > 0
    assert report.no_cross_project_facts is True
    assert report.deterministic_output is True


def test_compare_reports_fallback_when_fts_has_no_coverage(memory_project_ids):
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Echo fact has no populated FTS index.", category="style")

    report = rebuild_cli.compare_memory_fts(
        project_id,
        role="planner",
        request_context=RequestContext(title="uses"),
    )

    assert report.included_set_identical is True
    assert report.fts_coverage_count == 0
    assert report.fallback_count == 1
    assert report.deterministic_output is True


def test_seeded_compare_cleans_up_facts_suggestions_and_fts_rows(capsys):
    code = rebuild_cli.main(["compare", "--seed"])

    output = capsys.readouterr().out
    assert code == 0
    match = re.search(r"project_id=(fts-seed-[0-9a-f]+)", output)
    assert match is not None
    project_id = match.group(1)
    assert "included_set_identical=True" in output
    assert "mandatory_tier_identical=True" in output
    assert "deterministic_output=True" in output
    assert _memory_fact_rows(project_id) == []
    assert _memory_suggestion_rows(project_id) == []
    assert _fts_rows(project_id) == []


def test_compare_flag_off_entries_remain_current_rung_zero(
    monkeypatch,
    memory_project_ids,
):
    _require_fts5()
    project_id = _make_project_id(memory_project_ids)
    add_fact(project_id, "Foxtrot style fact uses Azure storage.", category="style")
    assert rebuild_cli.main(["rebuild", "--project-id", project_id, "--yes"]) == 0
    context = RequestContext(title="uses")
    monkeypatch.setattr(policy, "MEMORY_FTS_RETRIEVAL_ENABLED", False)

    current = prompt_builder.build_project_memory_block_detailed(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
    )
    compare_off_entries = rebuild_cli._included_entries(
        project_id,
        role="planner",
        token_budget=4000,
        request_context=context,
        fts_enabled=False,
    )

    assert compare_off_entries == tuple(
        rebuild_cli._snapshot_entry(entry) for entry in current.included_entries
    )
    assert policy.MEMORY_FTS_RETRIEVAL_ENABLED is False
