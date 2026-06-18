"""
PR-B role coverage for metadata-only LLM call provenance.

Fully mocked: no live provider calls, no Git, no approval gates. These tests pin
triage, planner, and summary/report_analyzer provenance writes as advisory
metadata-only rows that never change role execution.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.llm.base import LLMResponse
from backend.llm.role_config import Role
from backend.pipeline import llm_call_provenance_store as store
from backend.pipeline import planner, report_analyzer, triage
from backend.pipeline.llm_call_provenance_store import get_provenance_for_chunk

pytestmark = pytest.mark.unit

FEATURE_WITH_SENTINELS = (
    "Add ping endpoint PROMPT_SENTINEL RESPONSE_SENTINEL "
    "DIFF_SENTINEL RAW_ERROR_SENTINEL"
)

EXPECTED_PROVENANCE_COLUMNS = {
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

FORBIDDEN_CONTENT_MARKERS = {
    "PROMPT_SENTINEL",
    "RESPONSE_SENTINEL",
    "DIFF_SENTINEL",
    "RAW_ERROR_SENTINEL",
}


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
    run_id = f"prb-prov-test-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    return run_id


def _empty_memory_result(**kwargs):
    return SimpleNamespace(
        block="",
        role=kwargs.get("role"),
        token_budget=0,
        category_policy=(),
        included_entries=(),
        excluded_entries=(),
    )


def _triage_json(run_id: str, project_id: str) -> str:
    return json.dumps({
        "run_id": run_id,
        "project_id": project_id,
        "feature_description": FEATURE_WITH_SENTINELS,
        "complexity": "easy",
        "total_chunks": 1,
        "reasoning": "One safe chunk is enough.",
        "chunks": [{
            "chunk_number": 1,
            "title": "Add planning route",
            "description": "Create chunk planning endpoint.",
            "files_expected": ["backend/routes/chunks.py"],
            "depends_on": [],
            "risk_level": "low",
            "token_estimate": 1000,
            "requires_human_review": False,
            "rationale": "Small isolated API surface.",
        }],
    })


def _planner_json(run_id: str) -> str:
    return (
        "{"
        '"handoff_from": "planner",'
        '"handoff_to": "coder",'
        f'"run_id": "{run_id}",'
        f'"feature_description": "{FEATURE_WITH_SENTINELS}",'
        '"goal": "Create a ping endpoint.",'
        '"steps": ["Add route", "Return response"],'
        '"files_to_create": [],'
        '"files_to_modify": ["backend/main.py"],'
        '"files_to_read": [],'
        '"out_of_scope": [],'
        '"risks": [],'
        '"suggested_memory_entries": []'
        "}"
    )


VALID_ANALYZER_JSON = json.dumps({
    "summary": "Small FastAPI service that orchestrates chunked runs.",
    "findings": [{
        "title": "Broad exception handling",
        "severity": "medium",
        "confidence": "high",
        "file": "backend/app.py",
        "evidence": "Bare except blocks swallow errors.",
        "recommendation": "Narrow the caught exception types.",
    }],
    "limitations": ["Only a sample of files was reviewed."],
    "suggested_next_action": "Request a plan for the finding.",
})


def _response(
    text_out: str,
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    finish_reason: str = "stop",
) -> LLMResponse:
    return LLMResponse(
        text=text_out,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        raw={"ignored_raw_error": "RAW_ERROR_SENTINEL"},
    )


class _SequenceLLM:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.roles = []
        self.requests = []

    async def complete(self, role, request):
        self.roles.append(role)
        self.requests.append(request)
        assert self.responses, "Unexpected extra LLM call"
        return self.responses.pop(0)


def _patch_triage(monkeypatch, tmp_repo, llm: _SequenceLLM):
    monkeypatch.setattr(
        triage,
        "require_project",
        lambda project_id: {
            "id": project_id,
            "name": "Test Project",
            "repo_path": str(tmp_repo),
        },
    )
    monkeypatch.setattr(
        triage,
        "ensure_repo_indexed",
        lambda project_id, repo_path: {"status": "already_indexed"},
    )
    monkeypatch.setattr(
        triage,
        "get_relevant_files",
        lambda project_id, query, limit=20: [],
    )
    monkeypatch.setattr(
        triage, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(triage, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(triage, "complete_for_role", llm.complete)


def _patch_planner(monkeypatch, llm: _SequenceLLM):
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(planner, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(planner, "complete_for_role", llm.complete)


def _patch_report(monkeypatch, tmp_repo, llm: _SequenceLLM):
    monkeypatch.setattr(
        report_analyzer,
        "get_project",
        lambda project_id: {
            "id": project_id,
            "name": "Test Project",
            "repo_path": str(tmp_repo),
        },
    )
    monkeypatch.setattr(
        report_analyzer,
        "ensure_repo_indexed",
        lambda project_id, repo_path: {"status": "already_indexed"},
    )
    monkeypatch.setattr(
        report_analyzer,
        "get_relevant_files",
        lambda project_id, query, limit=20: [],
    )
    monkeypatch.setattr(
        report_analyzer,
        "_list_indexed_files",
        lambda project_id, limit: [],
    )
    monkeypatch.setattr(report_analyzer, "complete_for_role", llm.complete)


def _assert_row_metadata(
    row,
    *,
    run_id: str,
    role: str,
    chunk_number: int | None,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
):
    assert row.run_id == run_id
    assert row.role == role
    assert row.chunk_number == chunk_number
    assert row.provider == provider
    assert row.model == model
    assert row.finish_reason == "stop"
    assert row.input_tokens == input_tokens
    assert row.output_tokens == output_tokens
    assert row.selection_source is None


def _assert_rows_are_metadata_only(run_id: str):
    with engine.connect() as conn:
        table_info = conn.execute(
            text("PRAGMA table_info(llm_call_provenance)")
        ).fetchall()
        raw_rows = conn.execute(
            text("SELECT * FROM llm_call_provenance WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).fetchall()

    column_names = {row._mapping["name"] for row in table_info}
    assert column_names == EXPECTED_PROVENANCE_COLUMNS

    forbidden_columns = {
        "prompt",
        "response",
        "text",
        "diff",
        "content",
        "raw",
        "raw_error",
        "provider_error",
        "api_key",
        "token",
        "secret",
    }
    assert column_names.isdisjoint(forbidden_columns)

    for raw_row in raw_rows:
        values = " ".join(
            str(value) for value in raw_row._mapping.values() if value is not None
        )
        assert all(marker not in values for marker in FORBIDDEN_CONTENT_MARKERS)


@pytest.mark.asyncio
async def test_triage_persists_provenance_with_identity_and_tokens(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    project_id = "proj-triage"
    llm = _SequenceLLM([
        _response(
            _triage_json(run_id, project_id),
            provider="anthropic",
            model="claude-triage",
            input_tokens=11,
            output_tokens=22,
        )
    ])
    _patch_triage(monkeypatch, tmp_repo, llm)

    result = await triage.run_triage(run_id, project_id, FEATURE_WITH_SENTINELS)

    assert result.run_id == run_id
    rows = get_provenance_for_chunk(run_id, None, role=Role.TRIAGE.value)
    assert len(rows) == 1
    _assert_row_metadata(
        rows[0],
        run_id=run_id,
        role="triage",
        chunk_number=None,
        provider="anthropic",
        model="claude-triage",
        input_tokens=11,
        output_tokens=22,
    )
    _assert_rows_are_metadata_only(run_id)


@pytest.mark.asyncio
async def test_triage_provenance_failure_is_swallowed(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    project_id = "proj-triage"
    llm = _SequenceLLM([
        _response(
            _triage_json(run_id, project_id),
            provider="gemini",
            model="gemini-triage",
            input_tokens=1,
            output_tokens=2,
        )
    ])
    _patch_triage(monkeypatch, tmp_repo, llm)

    def _boom(_record):
        raise RuntimeError("simulated provenance DB failure with token sk-test")

    monkeypatch.setattr(store, "record_llm_call_provenance", _boom)

    result = await triage.run_triage(run_id, project_id, FEATURE_WITH_SENTINELS)

    assert result.run_id == run_id
    assert get_provenance_for_chunk(run_id, None, role=Role.TRIAGE.value) == []


@pytest.mark.asyncio
async def test_triage_retry_records_one_row_per_provider_call(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    project_id = "proj-triage"
    llm = _SequenceLLM([
        _response(
            "not json",
            provider="gemini",
            model="gemini-triage-invalid",
            input_tokens=1,
            output_tokens=2,
        ),
        _response(
            _triage_json(run_id, project_id),
            provider="gemini",
            model="gemini-triage-valid",
            input_tokens=3,
            output_tokens=4,
        ),
    ])
    _patch_triage(monkeypatch, tmp_repo, llm)

    result = await triage.run_triage(run_id, project_id, FEATURE_WITH_SENTINELS)

    assert result.run_id == run_id
    assert llm.roles == [Role.TRIAGE, Role.TRIAGE]
    rows = get_provenance_for_chunk(run_id, None, role=Role.TRIAGE.value)
    assert len(rows) == 2
    assert {row.model for row in rows} == {
        "gemini-triage-invalid",
        "gemini-triage-valid",
    }
    assert {row.input_tokens for row in rows} == {1, 3}


@pytest.mark.asyncio
async def test_planner_persists_provenance_with_identity_and_tokens(
    monkeypatch,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    chunk_number = 7
    llm = _SequenceLLM([
        _response(
            _planner_json(run_id),
            provider="openai",
            model="gpt-planner",
            input_tokens=33,
            output_tokens=44,
        )
    ])
    _patch_planner(monkeypatch, llm)

    result = await planner.run_planner(
        FEATURE_WITH_SENTINELS,
        run_id,
        chunk_number=chunk_number,
    )

    assert result.run_id == run_id
    rows = get_provenance_for_chunk(run_id, chunk_number, role=Role.PLANNER.value)
    assert len(rows) == 1
    _assert_row_metadata(
        rows[0],
        run_id=run_id,
        role="planner",
        chunk_number=chunk_number,
        provider="openai",
        model="gpt-planner",
        input_tokens=33,
        output_tokens=44,
    )
    _assert_rows_are_metadata_only(run_id)


@pytest.mark.asyncio
async def test_planner_provenance_failure_is_swallowed(monkeypatch, tracked_runs):
    run_id = _new_run(tracked_runs)
    chunk_number = 8
    llm = _SequenceLLM([
        _response(
            _planner_json(run_id),
            provider="deepseek",
            model="deepseek-planner",
            input_tokens=5,
            output_tokens=6,
        )
    ])
    _patch_planner(monkeypatch, llm)

    def _boom(_record):
        raise RuntimeError("simulated provenance DB failure with token sk-test")

    monkeypatch.setattr(store, "record_llm_call_provenance", _boom)

    result = await planner.run_planner(
        FEATURE_WITH_SENTINELS,
        run_id,
        chunk_number=chunk_number,
    )

    assert result.run_id == run_id
    assert get_provenance_for_chunk(run_id, chunk_number, Role.PLANNER.value) == []


@pytest.mark.asyncio
async def test_planner_retry_records_one_row_per_provider_call(
    monkeypatch,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    chunk_number = 9
    llm = _SequenceLLM([
        _response(
            "not json",
            provider="anthropic",
            model="claude-planner-invalid",
            input_tokens=7,
            output_tokens=8,
        ),
        _response(
            _planner_json(run_id),
            provider="anthropic",
            model="claude-planner-valid",
            input_tokens=9,
            output_tokens=10,
        ),
    ])
    _patch_planner(monkeypatch, llm)

    result = await planner.run_planner(
        FEATURE_WITH_SENTINELS,
        run_id,
        chunk_number=chunk_number,
    )

    assert result.run_id == run_id
    assert llm.roles == [Role.PLANNER, Role.PLANNER]
    rows = get_provenance_for_chunk(run_id, chunk_number, role=Role.PLANNER.value)
    assert len(rows) == 2
    assert {row.model for row in rows} == {
        "claude-planner-invalid",
        "claude-planner-valid",
    }
    assert {row.output_tokens for row in rows} == {8, 10}


@pytest.mark.asyncio
async def test_summary_persists_provenance_with_identity_and_tokens(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    llm = _SequenceLLM([
        _response(
            VALID_ANALYZER_JSON,
            provider="gemini",
            model="gemini-summary",
            input_tokens=55,
            output_tokens=66,
        )
    ])
    _patch_report(monkeypatch, tmp_repo, llm)

    result = await report_analyzer.run_report_analysis(
        run_id,
        "proj-summary",
        FEATURE_WITH_SENTINELS,
    )

    assert result.report_result is not None
    rows = get_provenance_for_chunk(run_id, None, role=Role.SUMMARY.value)
    assert len(rows) == 1
    _assert_row_metadata(
        rows[0],
        run_id=run_id,
        role="summary",
        chunk_number=None,
        provider="gemini",
        model="gemini-summary",
        input_tokens=55,
        output_tokens=66,
    )
    _assert_rows_are_metadata_only(run_id)


@pytest.mark.asyncio
async def test_summary_provenance_failure_is_swallowed(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    llm = _SequenceLLM([
        _response(
            VALID_ANALYZER_JSON,
            provider="openai",
            model="gpt-summary",
            input_tokens=1,
            output_tokens=2,
        )
    ])
    _patch_report(monkeypatch, tmp_repo, llm)

    def _boom(_record):
        raise RuntimeError("simulated provenance DB failure with token sk-test")

    monkeypatch.setattr(store, "record_llm_call_provenance", _boom)

    result = await report_analyzer.run_report_analysis(
        run_id,
        "proj-summary",
        FEATURE_WITH_SENTINELS,
    )

    assert result.report_result is not None
    assert get_provenance_for_chunk(run_id, None, role=Role.SUMMARY.value) == []


@pytest.mark.asyncio
async def test_summary_retry_records_one_row_per_provider_call(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id = _new_run(tracked_runs)
    llm = _SequenceLLM([
        _response(
            "not json",
            provider="deepseek",
            model="deepseek-summary-invalid",
            input_tokens=13,
            output_tokens=14,
        ),
        _response(
            VALID_ANALYZER_JSON,
            provider="deepseek",
            model="deepseek-summary-valid",
            input_tokens=15,
            output_tokens=16,
        ),
    ])
    _patch_report(monkeypatch, tmp_repo, llm)

    result = await report_analyzer.run_report_analysis(
        run_id,
        "proj-summary",
        FEATURE_WITH_SENTINELS,
    )

    assert result.report_result is not None
    assert llm.roles == [Role.SUMMARY, Role.SUMMARY]
    rows = get_provenance_for_chunk(run_id, None, role=Role.SUMMARY.value)
    assert len(rows) == 2
    assert {row.model for row in rows} == {
        "deepseek-summary-invalid",
        "deepseek-summary-valid",
    }
    assert {row.input_tokens for row in rows} == {13, 15}
