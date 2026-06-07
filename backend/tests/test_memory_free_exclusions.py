"""
test_memory_free_exclusions.py
Tests for M3F2a: read-only structured surfacing of "free" memory exclusions
(category_not_allowed_for_role + budget_dropped) from the already-loaded active
facts, WITHOUT changing the rendered prompt block or which facts are injected.

Scope guard for this slice:
  - Prompt block string stays byte-identical (modulo the Generated timestamp).
  - Included selection is unchanged.
  - The active-only SQL is NOT widened: stale/archived/historical facts are never
    loaded and never appear as excluded entries (deferred to M3F2b).
"""

import inspect
import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.memory.memory_store import add_fact, archive_fact
from backend.memory.injection_store import (
    compute_entries_hash,
    list_memory_injection_events,
    record_memory_injection_event,
)
from backend.memory.prompt_builder import (
    EXCLUSION_BUDGET_DROPPED,
    EXCLUSION_CATEGORY_NOT_ALLOWED,
    build_project_memory_block,
    build_project_memory_block_detailed,
)

pytestmark = pytest.mark.unit


# --- helpers ----------------------------------------------------------------

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
            conn.execute(
                text("DELETE FROM memory_injection_events WHERE project_id = :p"),
                {"p": project_id},
            )


def _new_project(project_ids, label="m3f2a"):
    project_id = f"{label}-{uuid.uuid4().hex}"
    project_ids.append(project_id)
    return project_id


def _strip_generated(block: str) -> str:
    return "\n".join(
        line for line in block.splitlines() if not line.startswith("Generated:")
    )


def _mark_status(memory_id: str, status: str, is_stale: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE memory_facts SET status = :s, is_stale = :st WHERE id = :id"
            ),
            {"s": status, "st": is_stale, "id": memory_id},
        )


# triage policy excludes style / reviewer_pref / other / architecture / deploy,
# so those make reliable "category not allowed for role" facts.
def _seed_triage_in_policy(project_id: str) -> None:
    add_fact(project_id, "Stack rule: backend uses FastAPI", category="stack", priority=10)
    add_fact(project_id, "Test rule: run pytest backend tests", category="test", priority=20)
    add_fact(project_id, "Structure rule: routes live under backend", category="structure", priority=30)


def _seed_triage_out_of_policy(project_id: str) -> None:
    add_fact(project_id, "Style rule: prefer concise labels", category="style", priority=40)
    add_fact(project_id, "Reviewer pref: mention rollback risk", category="reviewer_pref", priority=50)
    add_fact(project_id, "Other note: rejected approach reuse helper", category="other", priority=60)


# --- byte-identical block ---------------------------------------------------

def test_block_is_byte_identical_when_out_of_policy_facts_present(project_ids):
    # Project A: in-policy facts PLUS out-of-policy facts.
    # Project B: only the same in-policy facts.
    # The rendered block must be byte-identical (modulo the Generated line):
    # out-of-policy facts must not perturb selection, ordering, or budget math.
    project_a = _new_project(project_ids, "m3f2a-all")
    project_b = _new_project(project_ids, "m3f2a-inpolicy")
    _seed_triage_in_policy(project_a)
    _seed_triage_out_of_policy(project_a)
    _seed_triage_in_policy(project_b)

    block_a = build_project_memory_block(
        project_id=project_a, role="triage", project_name="Demo"
    )
    block_b = build_project_memory_block(
        project_id=project_b, role="triage", project_name="Demo"
    )

    assert block_a != ""
    assert _strip_generated(block_a) == _strip_generated(block_b)
    # And the out-of-policy content never leaks into the rendered block.
    assert "concise labels" not in block_a
    assert "rollback risk" not in block_a
    assert "reuse helper" not in block_a


def test_included_entries_carry_no_exclusion_reason(project_ids):
    project_id = _new_project(project_ids)
    _seed_triage_in_policy(project_id)
    _seed_triage_out_of_policy(project_id)

    result = build_project_memory_block_detailed(project_id=project_id, role="triage")

    assert result.included_count >= 1
    for entry in result.included_entries:
        assert entry.exclusion_reason is None
        assert entry.content in result.block


# --- category_not_allowed_for_role -----------------------------------------

def test_category_not_allowed_entries_are_surfaced(project_ids):
    project_id = _new_project(project_ids)
    _seed_triage_in_policy(project_id)
    _seed_triage_out_of_policy(project_id)

    result = build_project_memory_block_detailed(project_id=project_id, role="triage")

    category_excluded = [
        e
        for e in result.excluded_entries
        if e.exclusion_reason == EXCLUSION_CATEGORY_NOT_ALLOWED
    ]
    contents = {e.content for e in category_excluded}
    assert "Style rule: prefer concise labels" in contents
    assert "Reviewer pref: mention rollback risk" in contents
    assert "Other note: rejected approach reuse helper" in contents
    # In-policy facts are never reported as category-excluded.
    assert all("backend uses FastAPI" != e.content for e in category_excluded)
    # Every category-excluded entry was an active fact at injection time.
    assert all(e.status_at_injection == "active" for e in category_excluded)


def test_category_excluded_surfaced_even_when_no_in_policy_facts(project_ids):
    # A role with zero in-policy facts still builds an empty block, but the
    # out-of-policy active facts are surfaced as category exclusions.
    project_id = _new_project(project_ids)
    _seed_triage_out_of_policy(project_id)

    result = build_project_memory_block_detailed(project_id=project_id, role="triage")

    assert result.block == ""
    assert result.included_entries == ()
    assert len(result.excluded_entries) == 3
    assert all(
        e.exclusion_reason == EXCLUSION_CATEGORY_NOT_ALLOWED
        for e in result.excluded_entries
    )


# --- budget_dropped + distinct reasons --------------------------------------

def test_budget_dropped_and_category_excluded_are_distinct_reasons(project_ids):
    project_id = _new_project(project_ids)
    # Several in-policy (stack) facts under a tiny budget -> some budget-dropped.
    for i in range(5):
        add_fact(project_id, f"Stack rule number {i}: backend uses FastAPI here", category="stack", priority=100 + i)
    # One out-of-policy (style) fact for triage -> category-excluded.
    add_fact(project_id, "Style rule: prefer concise labels", category="style", priority=200)

    result = build_project_memory_block_detailed(
        project_id=project_id, role="triage", token_budget=14
    )

    reasons = {e.exclusion_reason for e in result.excluded_entries}
    assert EXCLUSION_BUDGET_DROPPED in reasons
    assert EXCLUSION_CATEGORY_NOT_ALLOWED in reasons
    # Budget-dropped entries are in-policy active stack facts, not the style fact.
    budget_dropped = [
        e for e in result.excluded_entries if e.exclusion_reason == EXCLUSION_BUDGET_DROPPED
    ]
    assert budget_dropped
    assert all(e.category == "stack" for e in budget_dropped)
    # No excluded entry leaked into the rendered block.
    for entry in result.excluded_entries:
        assert entry.content not in result.block


# --- SQL not widened: status-excluded facts never loaded --------------------

def test_status_excluded_facts_are_not_loaded_or_surfaced(project_ids):
    project_id = _new_project(project_ids)
    add_fact(project_id, "Stack rule: active backend uses FastAPI", category="stack")
    archived = add_fact(project_id, "Stack rule: archived db note", category="stack")
    stale = add_fact(project_id, "Stack rule: stale db note", category="stack")
    historical = add_fact(project_id, "Stack rule: historical db note", category="stack")
    archive_fact(project_id, archived["id"], "No longer true")
    _mark_status(stale["id"], "stale", 1)
    _mark_status(historical["id"], "historical", 0)

    result = build_project_memory_block_detailed(project_id=project_id, role="triage")

    # Active fact rendered; inactive facts absent from the block (unchanged).
    assert "active backend uses FastAPI" in result.block
    assert "archived db note" not in result.block
    assert "stale db note" not in result.block
    assert "historical db note" not in result.block

    # M3F2a does NOT widen the query: inactive facts are not loaded, so they are
    # never surfaced as excluded entries either.
    excluded_contents = {e.content for e in result.excluded_entries}
    assert "Stack rule: archived db note" not in excluded_contents
    assert "Stack rule: stale db note" not in excluded_contents
    assert "Stack rule: historical db note" not in excluded_contents
    # No status_excluded_* reason is produced in this slice.
    assert all(
        e.exclusion_reason
        in {EXCLUSION_BUDGET_DROPPED, EXCLUSION_CATEGORY_NOT_ALLOWED}
        for e in result.excluded_entries
    )


# --- runtime provenance persistence -----------------------------------------

def test_provenance_persists_category_excluded_entries(project_ids):
    project_id = _new_project(project_ids)
    _seed_triage_in_policy(project_id)
    _seed_triage_out_of_policy(project_id)
    run_id = f"m3f2a-run-{uuid.uuid4().hex}"

    result = build_project_memory_block_detailed(project_id=project_id, role="triage")
    record_memory_injection_event(
        run_id=run_id,
        project_id=project_id,
        role="triage",
        token_budget=result.token_budget,
        category_policy=list(result.category_policy),
        included_entries=[e.__dict__ for e in result.included_entries],
        excluded_entries=[e.__dict__ for e in result.excluded_entries],
    )

    events = list_memory_injection_events(run_id, project_id=project_id)
    assert len(events) == 1
    excluded = events[0]["excluded_entries"]
    reasons = {e.get("exclusion_reason") for e in excluded}
    assert EXCLUSION_CATEGORY_NOT_ALLOWED in reasons
    # Stored excluded entries keep their reason and content.
    style = [e for e in excluded if e.get("content") == "Style rule: prefer concise labels"]
    assert style and style[0]["exclusion_reason"] == EXCLUSION_CATEGORY_NOT_ALLOWED


def test_entries_hash_ignores_excluded_entries(project_ids):
    # entries_hash digests INCLUDED-entry identity only; adding/removing excluded
    # entries must not change it.
    project_id = _new_project(project_ids)
    _seed_triage_in_policy(project_id)
    _seed_triage_out_of_policy(project_id)
    run_id = f"m3f2a-hash-{uuid.uuid4().hex}"

    result = build_project_memory_block_detailed(project_id=project_id, role="triage")
    included_dicts = [e.__dict__ for e in result.included_entries]

    with_excluded = record_memory_injection_event(
        run_id=run_id, project_id=project_id, role="triage",
        token_budget=result.token_budget, category_policy=list(result.category_policy),
        included_entries=included_dicts,
        excluded_entries=[e.__dict__ for e in result.excluded_entries],
    )
    without_excluded = record_memory_injection_event(
        run_id=run_id, project_id=project_id, role="coder",
        token_budget=result.token_budget, category_policy=list(result.category_policy),
        included_entries=included_dicts,
        excluded_entries=[],
    )

    assert with_excluded["entries_hash"] == without_excluded["entries_hash"]
    assert with_excluded["entries_hash"] == compute_entries_hash(included_dicts)


# --- no mutation ------------------------------------------------------------

def test_building_block_does_not_mutate_memory_facts(project_ids):
    project_id = _new_project(project_ids)
    _seed_triage_in_policy(project_id)
    _seed_triage_out_of_policy(project_id)

    def snapshot():
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, status, is_stale FROM memory_facts "
                    "WHERE project_id = :p ORDER BY id"
                ),
                {"p": project_id},
            ).fetchall()
        return [tuple(r) for r in rows]

    before = snapshot()
    build_project_memory_block_detailed(project_id=project_id, role="triage")
    build_project_memory_block_detailed(project_id=project_id, role="coder")
    after = snapshot()

    assert before == after


# --- reviewer/summary remain unwired at runtime -----------------------------

def test_reviewer_pipeline_does_not_capture_injection():
    # Guard: M3F2a must not wire reviewer memory injection. The reviewer pipeline
    # module must not import the injection capture or the memory block builder.
    from backend.pipeline import reviewer

    reviewer_src = inspect.getsource(reviewer)
    assert "capture_memory_injection" not in reviewer_src
    assert "build_project_memory_block" not in reviewer_src
