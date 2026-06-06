"""
test_memory_injection_provenance.py
Tests for M3C1: persisted memory injection provenance + read-only endpoint.

Covers the pure detailed builder (byte-identical block, same-computation detail,
deterministic entries_hash, budget-dropped capture), the append-only store
(insert/list, project scoping, empty, redaction), the read-only endpoint
(returns/filters/404/content_hash stripping), and best-effort runtime capture
(planner/coder record events; a recorder failure never fails the run).
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.llm.base import LLMResponse
from backend.main import app
from backend.memory import injection_store
from backend.memory.injection_store import (
    capture_memory_injection,
    compute_entries_hash,
    list_memory_injection_events,
    record_memory_injection_event,
)
from backend.memory.memory_store import add_fact
from backend.memory.prompt_builder import (
    build_project_memory_block,
    build_project_memory_block_detailed,
)
from backend.pipeline import planner

pytestmark = pytest.mark.unit


# --- helpers ----------------------------------------------------------------

def _strip_generated(block: str) -> str:
    return "\n".join(
        line for line in block.splitlines() if not line.startswith("Generated:")
    )


def _cleanup_project(project_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM memory_facts WHERE project_id = :p"),
            {"p": project_id},
        )
        conn.execute(
            text("DELETE FROM memory_injection_events WHERE project_id = :p"),
            {"p": project_id},
        )


def _insert_run(run_id: str, project_id: str) -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs (id, project_id, feature_description, status)
            VALUES (:id, :project_id, :feature, 'running')
        """), {"id": run_id, "project_id": project_id, "feature": "test feature"})


def _delete_run(run_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM pipeline_runs WHERE id = :id"), {"id": run_id}
        )
        conn.execute(
            text("DELETE FROM memory_injection_events WHERE run_id = :id"),
            {"id": run_id},
        )


def _entry(fact_id="f1", content="Tests use pytest.", category="test", scope="tests"):
    return {
        "fact_id": fact_id,
        "content": content,
        "content_hash": "hash-" + fact_id,
        "category": category,
        "scope": scope,
        "priority": 100,
        "status_at_injection": "active",
    }


# --- pure detailed builder --------------------------------------------------

def test_detailed_block_matches_plain_block_modulo_timestamp():
    project_id = f"mi-builder-{uuid.uuid4().hex}"
    add_fact(project_id, "Backend uses FastAPI.", category="stack")
    try:
        plain = build_project_memory_block(project_id=project_id, role="planner")
        detailed = build_project_memory_block_detailed(
            project_id=project_id, role="planner"
        )
        assert detailed.block != ""
        # Only the generated timestamp line may differ between two calls.
        assert _strip_generated(detailed.block) == _strip_generated(plain)
    finally:
        _cleanup_project(project_id)


def test_detailed_includes_entry_metadata_from_same_computation():
    project_id = f"mi-meta-{uuid.uuid4().hex}"
    add_fact(project_id, "Backend uses FastAPI.", category="stack", scope="backend")
    try:
        result = build_project_memory_block_detailed(
            project_id=project_id, role="planner"
        )
        assert result.included_count == 1
        entry = result.included_entries[0]
        assert entry.fact_id is not None
        assert entry.content == "Backend uses FastAPI."
        assert entry.content_hash is not None
        assert entry.category == "stack"
        assert entry.scope == "backend"
        assert entry.status_at_injection == "active"
        # Each included entry's content appears in the rendered block.
        assert entry.content in result.block
    finally:
        _cleanup_project(project_id)


def test_detailed_budget_dropped_entries_are_captured_as_excluded():
    project_id = f"mi-budget-{uuid.uuid4().hex}"
    for i in range(6):
        add_fact(project_id, f"Backend convention number {i} for tests.", category="style")
    try:
        # Tiny budget forces in-policy facts to be dropped.
        result = build_project_memory_block_detailed(
            project_id=project_id, role="planner", token_budget=12
        )
        assert result.excluded_count >= 1
        # Dropped facts are NOT rendered into the block.
        for entry in result.excluded_entries:
            assert entry.content not in result.block
    finally:
        _cleanup_project(project_id)


def test_empty_project_returns_empty_result():
    result = build_project_memory_block_detailed(project_id="", role="planner")
    assert result.block == ""
    assert result.included_entries == ()
    assert result.excluded_entries == ()


# --- entries_hash -----------------------------------------------------------

def test_entries_hash_is_order_sensitive_and_deterministic():
    a = _entry("f1", "Tests use pytest.")
    b = _entry("f2", "Backend uses FastAPI.")
    assert compute_entries_hash([a, b]) == compute_entries_hash([a, b])
    assert compute_entries_hash([a, b]) != compute_entries_hash([b, a])


def test_entries_hash_excludes_timestamp_via_record():
    # Two events with identical included entries must share an entries_hash even
    # though they are created at different times (header timestamp excluded).
    run_id = f"mi-hash-{uuid.uuid4().hex}"
    project_id = f"mi-hashp-{uuid.uuid4().hex}"
    try:
        first = record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="planner",
            token_budget=1200, category_policy=["stack"],
            included_entries=[_entry()], excluded_entries=[],
        )
        second = record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="coder",
            token_budget=1200, category_policy=["stack"],
            included_entries=[_entry()], excluded_entries=[],
        )
        assert first["entries_hash"] == second["entries_hash"]
        assert first["created_at"] is not None
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


# --- store: record / list / scope / append-only -----------------------------

def test_record_and_list_event_roundtrip():
    run_id = f"mi-rt-{uuid.uuid4().hex}"
    project_id = f"mi-rtp-{uuid.uuid4().hex}"
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="planner",
            chunk_number=1, token_budget=1200, category_policy=["stack", "test"],
            included_entries=[_entry()], excluded_entries=[_entry("f2", "Run tests with pytest.")],
        )
        events = list_memory_injection_events(run_id, project_id=project_id)
        assert len(events) == 1
        event = events[0]
        assert event["run_id"] == run_id
        assert event["role"] == "planner"
        assert event["chunk_number"] == 1
        assert event["included_count"] == 1
        assert event["excluded_count"] == 1
        assert event["category_policy"] == ["stack", "test"]
        assert event["included_entries"][0]["content"] == "Tests use pytest."
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


def test_list_is_project_scoped():
    run_id = f"mi-scope-{uuid.uuid4().hex}"
    project_a = f"mi-a-{uuid.uuid4().hex}"
    project_b = f"mi-b-{uuid.uuid4().hex}"
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_a, role="planner",
            token_budget=1200, category_policy=["stack"], included_entries=[_entry()],
        )
        # A different project's event (same run_id is implausible, but proves the
        # project_id filter is applied).
        record_memory_injection_event(
            run_id=run_id, project_id=project_b, role="coder",
            token_budget=1200, category_policy=["stack"], included_entries=[_entry()],
        )
        only_a = list_memory_injection_events(run_id, project_id=project_a)
        assert len(only_a) == 1
        assert only_a[0]["project_id"] == project_a
    finally:
        _delete_run(run_id)
        _cleanup_project(project_a)
        _cleanup_project(project_b)


def test_list_empty_for_unknown_run():
    assert list_memory_injection_events(f"missing-{uuid.uuid4().hex}") == []


def test_append_only_attempt_number_increments():
    run_id = f"mi-attempt-{uuid.uuid4().hex}"
    project_id = f"mi-attemptp-{uuid.uuid4().hex}"
    try:
        first = record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="coder", chunk_number=1,
            token_budget=1200, category_policy=["stack"], included_entries=[_entry()],
        )
        second = record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="coder", chunk_number=1,
            token_budget=1200, category_policy=["stack"], included_entries=[_entry()],
        )
        assert first["attempt_number"] == 1
        assert second["attempt_number"] == 2
        assert len(list_memory_injection_events(run_id, project_id=project_id)) == 2
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


def test_store_has_no_update_or_delete_api():
    # Append-only: the module must not expose mutation of existing rows.
    for name in ("update_memory_injection_event", "delete_memory_injection_event"):
        assert not hasattr(injection_store, name)


def test_record_requires_project_id():
    with pytest.raises(ValueError):
        record_memory_injection_event(
            run_id="r", project_id="  ", role="planner",
            token_budget=1, category_policy=[], included_entries=[],
        )


# --- safety: defense-in-depth redaction -------------------------------------

def test_unsafe_content_is_redacted_not_stored():
    run_id = f"mi-safe-{uuid.uuid4().hex}"
    project_id = f"mi-safep-{uuid.uuid4().hex}"
    unsafe = _entry("bad", "sk-ABCDEFGHIJKLMNOP1234567890 leaked key")
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="planner",
            token_budget=1200, category_policy=["stack"], included_entries=[unsafe],
        )
        events = list_memory_injection_events(run_id, project_id=project_id)
        stored = events[0]["included_entries"][0]["content"]
        assert "sk-ABCDEFGHIJKLMNOP" not in stored
        assert "redacted" in stored.lower()
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


# --- best-effort capture ----------------------------------------------------

def test_capture_swallows_recorder_failure(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(injection_store, "record_memory_injection_event", _boom)

    class _Result:
        block = ""
        token_budget = 0
        category_policy = ()
        included_entries = ()
        excluded_entries = ()

    # Must not raise, must return None.
    assert capture_memory_injection(
        _Result(), run_id="r", project_id="p", role="planner"
    ) is None


# --- read-only endpoint -----------------------------------------------------

def test_endpoint_returns_events_and_strips_content_hash():
    client = TestClient(app)
    run_id = f"mi-ep-{uuid.uuid4().hex}"
    project_id = f"mi-epp-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="planner", chunk_number=2,
            token_budget=1200, category_policy=["stack"], included_entries=[_entry()],
        )
        resp = client.get(f"/api/v1/runs/{run_id}/memory-injections")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert len(body["events"]) == 1
        event = body["events"][0]
        assert event["role"] == "planner"
        assert event["chunk_number"] == 2
        assert event["entries_hash"]  # event-level integrity digest retained
        entry = event["included_entries"][0]
        assert entry["content"] == "Tests use pytest."
        # content_hash stripped from per-entry output for parity with memory API.
        assert "content_hash" not in entry
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


def test_endpoint_filters_by_role_and_chunk():
    client = TestClient(app)
    run_id = f"mi-epf-{uuid.uuid4().hex}"
    project_id = f"mi-epfp-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="triage", chunk_number=None,
            token_budget=400, category_policy=["stack"], included_entries=[_entry()],
        )
        record_memory_injection_event(
            run_id=run_id, project_id=project_id, role="coder", chunk_number=1,
            token_budget=1200, category_policy=["stack"], included_entries=[_entry()],
        )
        coder_only = client.get(
            f"/api/v1/runs/{run_id}/memory-injections", params={"role": "coder"}
        ).json()
        assert len(coder_only["events"]) == 1
        assert coder_only["events"][0]["role"] == "coder"

        chunk_one = client.get(
            f"/api/v1/runs/{run_id}/memory-injections", params={"chunk_number": 1}
        ).json()
        assert len(chunk_one["events"]) == 1
        assert chunk_one["events"][0]["chunk_number"] == 1
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


def test_endpoint_404_for_unknown_run():
    client = TestClient(app)
    resp = client.get(f"/api/v1/runs/does-not-exist-{uuid.uuid4().hex}/memory-injections")
    assert resp.status_code == 404


def test_endpoint_empty_for_run_without_provenance():
    client = TestClient(app)
    run_id = f"mi-epe-{uuid.uuid4().hex}"
    project_id = f"mi-epep-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    try:
        resp = client.get(f"/api/v1/runs/{run_id}/memory-injections")
        assert resp.status_code == 200
        assert resp.json()["events"] == []
    finally:
        _delete_run(run_id)


# --- runtime capture (planner / coder) --------------------------------------

def _planner_json(run_id: str) -> str:
    return (
        "{"
        '"handoff_from": "planner", "handoff_to": "coder",'
        f'"run_id": "{run_id}",'
        '"feature_description": "x", "goal": "g", "steps": ["a"],'
        '"files_to_create": [], "files_to_modify": ["backend/main.py"],'
        '"files_to_read": [], "out_of_scope": [], "risks": [],'
        '"suggested_memory_entries": []}'
    )


@pytest.mark.asyncio
async def test_planner_records_injection_event(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"mi-planrun-{uuid.uuid4().hex}"
    add_fact(project_id, "Backend uses FastAPI.", category="stack")

    async def _fake_complete(role, request, overrides=None):
        return LLMResponse(
            text=_planner_json(run_id), provider="fake", model="m",
            input_tokens=1, output_tokens=1, finish_reason="stop",
        )

    monkeypatch.setattr(planner, "complete_for_role", _fake_complete)
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)

    try:
        await planner.run_planner("feature", run_id, chunk_number=1, project_id=project_id)
        events = list_memory_injection_events(run_id, project_id=project_id, role="planner")
        assert len(events) == 1
        assert events[0]["role"] == "planner"
        assert events[0]["chunk_number"] == 1
        assert events[0]["included_count"] >= 1
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)


@pytest.mark.asyncio
async def test_planner_run_survives_capture_failure(monkeypatch):
    run_id = str(uuid.uuid4())
    project_id = f"mi-planfail-{uuid.uuid4().hex}"
    add_fact(project_id, "Backend uses FastAPI.", category="stack")

    async def _fake_complete(role, request, overrides=None):
        return LLMResponse(
            text=_planner_json(run_id), provider="fake", model="m",
            input_tokens=1, output_tokens=1, finish_reason="stop",
        )

    def _boom(**kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(planner, "complete_for_role", _fake_complete)
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)
    # Force the underlying recorder to fail; capture must swallow it.
    monkeypatch.setattr(injection_store, "record_memory_injection_event", _boom)

    try:
        handoff = await planner.run_planner(
            "feature", run_id, chunk_number=1, project_id=project_id
        )
        assert handoff.run_id == run_id  # run completed despite capture failure
        assert list_memory_injection_events(run_id, project_id=project_id) == []
    finally:
        _delete_run(run_id)
        _cleanup_project(project_id)
