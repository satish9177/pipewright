"""
test_llm_call_provenance_store.py
Storage tests for the isolated, metadata-only LLM call provenance store (#33B).

Pure backend DB CRUD: no git, no LLM, no routes. Rows are seeded directly. Run ids
are synthetic (SQLite FK enforcement is off in this engine), keeping these tests
isolated from runs/chunks/checkpoints. Cleanup only touches llm_call_provenance.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline import llm_call_provenance_store as store
from backend.pipeline.llm_call_provenance_store import (
    LLMCallProvenanceRecord,
    get_provenance_for_chunk,
    record_llm_call_provenance,
    try_record_llm_call_provenance,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    init_db()
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(
                text("DELETE FROM llm_call_provenance WHERE run_id = :run_id"),
                {"run_id": run_id},
            )


def _new_run(tracked_runs) -> str:
    run_id = f"prov-test-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    return run_id


def test_record_and_read_back_full_metadata(tracked_runs):
    run_id = _new_run(tracked_runs)
    stored = record_llm_call_provenance(LLMCallProvenanceRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=3,
        role="coder",
        provider="anthropic",
        model="claude-x",
        finish_reason="stop",
        input_tokens=11,
        output_tokens=22,
    ))

    assert stored.run_id == run_id
    assert stored.chunk_number == 3
    assert stored.role == "coder"
    assert stored.provider == "anthropic"
    assert stored.model == "claude-x"
    assert stored.finish_reason == "stop"
    assert stored.input_tokens == 11
    assert stored.output_tokens == 22
    assert stored.created_at  # generated when blank

    rows = get_provenance_for_chunk(run_id, 3, role="coder")
    assert len(rows) == 1
    assert rows[0].provider == "anthropic"
    assert rows[0].model == "claude-x"


def test_nullable_token_and_finish_reason_round_trip(tracked_runs):
    run_id = _new_run(tracked_runs)
    record_llm_call_provenance(LLMCallProvenanceRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=1,
        role="coder",
        provider="gemini",
        model="gemini-2.5-flash-lite",
        finish_reason=None,
        input_tokens=None,
        output_tokens=None,
    ))

    rows = get_provenance_for_chunk(run_id, 1)
    assert len(rows) == 1
    assert rows[0].finish_reason is None
    assert rows[0].input_tokens is None
    assert rows[0].output_tokens is None
    # selection_source is reserved/unused for now and must persist as NULL.
    assert rows[0].selection_source is None


def test_null_chunk_number_round_trips_and_matches_is_null(tracked_runs):
    run_id = _new_run(tracked_runs)
    record_llm_call_provenance(LLMCallProvenanceRecord(
        id=str(uuid.uuid4()),
        run_id=run_id,
        chunk_number=None,
        role="triage",
        provider="gemini",
        model="gemini-2.5-flash-lite",
    ))

    rows = get_provenance_for_chunk(run_id, None)
    assert len(rows) == 1
    assert rows[0].chunk_number is None
    # A specific chunk_number must NOT match the NULL row.
    assert get_provenance_for_chunk(run_id, 5) == []


def test_try_record_returns_record_on_success(tracked_runs):
    run_id = _new_run(tracked_runs)
    result = try_record_llm_call_provenance(
        run_id=run_id,
        chunk_number=2,
        role="coder",
        provider="openai",
        model="gpt-x",
        finish_reason="stop",
        input_tokens=5,
        output_tokens=7,
    )
    assert result is not None
    assert result.provider == "openai"
    assert get_provenance_for_chunk(run_id, 2)[0].model == "gpt-x"


def test_try_record_swallows_failure_and_writes_nothing(tracked_runs, monkeypatch):
    run_id = _new_run(tracked_runs)

    def _boom(_record):
        raise RuntimeError("simulated DB failure")

    # Force the strict writer to fail; the best-effort wrapper must swallow it.
    monkeypatch.setattr(store, "record_llm_call_provenance", _boom)

    result = try_record_llm_call_provenance(
        run_id=run_id,
        chunk_number=1,
        role="coder",
        provider="gemini",
        model="gemini-2.5-flash-lite",
    )

    assert result is None  # swallowed, no raise
    assert get_provenance_for_chunk(run_id, 1) == []  # nothing persisted


def test_table_is_metadata_only_no_content_columns():
    """The table must have no column capable of holding prompts/responses/diffs/secrets."""
    init_db()
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(llm_call_provenance)")).fetchall()
    columns = {row._mapping["name"] for row in rows}
    assert columns == {
        "id",
        "run_id",
        "chunk_number",
        "role",
        "provider",
        "model",
        "selection_source",
        "finish_reason",
        "input_tokens",
        "output_tokens",
        "created_at",
    }


def test_record_model_has_no_content_fields():
    """The Pydantic record exposes only metadata fields — no prompt/response/diff."""
    fields = set(LLMCallProvenanceRecord.model_fields)
    forbidden = {"prompt", "response", "text", "diff", "content", "api_key", "error"}
    assert fields.isdisjoint(forbidden)
