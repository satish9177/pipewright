"""
test_reviewer_provenance.py
Reviewer coverage for metadata-only LLM call provenance.

Fully mocked: no real LLM, no real git. The reviewer keeps its LLMResponse, so
it records one best-effort provenance row after a successful provider call. The
row stores only provider/model/token metadata and never changes reviewer output.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import engine, init_db
from backend.llm.base import LLMResponse
from backend.llm.role_config import Role
from backend.models.chunk import ChunkDefinition
from backend.models.handoff import (
    CoderHandoff,
    FileChange,
    PatchResult,
    PipelineTestResult,
)
from backend.pipeline import llm_call_provenance_store as store
from backend.pipeline import reviewer
from backend.pipeline.llm_call_provenance_store import get_provenance_for_chunk
from backend.pipeline.reviewer import run_chunk_review
from backend.pipeline.reviewer_models import ChunkReviewStatus, ChunkReviewVerdict

pytestmark = pytest.mark.unit


VALID_REVIEW_JSON = json.dumps({
    "verdict": "approve_with_notes",
    "summary": "Looks reasonable.",
    "findings": [],
    "test_gap_summary": "",
    "scope_summary": "",
    "security_or_safety_summary": "",
    "recommended_human_action": "",
})


@pytest.fixture()
def tracked_runs():
    init_db()
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in ("llm_call_provenance", "chunk_reviews", "checkpoints"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )


def _seed_run_with_test_checkpoint(
    tracked_runs,
    git_hash: str,
    chunk_number: int = 1,
) -> str:
    run_id = f"reviewer-prov-test-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    save_checkpoint(
        run_id=run_id,
        step="test",
        output={},
        handoff_contract={},
        git_hash=git_hash,
        tests_passed=True,
        step_completed=True,
        chunk_number=chunk_number,
    )
    return run_id


def _chunk(chunk_number: int = 1) -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=chunk_number,
        title="Add hello",
        description="Add a hello function",
        files_expected=["src/app.py"],
        depends_on=[],
        risk_level="low",
        token_estimate=10,
        requires_human_review=False,
        rationale="r",
    )


def _code(run_id: str) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="Add a hello function",
        files_changed=[
            FileChange(
                path="src/app.py",
                action="edit",
                old_string="x",
                new_string="y",
                reason="add hello",
            )
        ],
        summary="Added hello.",
    )


def _patch(run_id: str, git_hash: str) -> PatchResult:
    return PatchResult(
        run_id=run_id,
        success=True,
        diff="--- a/src/app.py\n+++ b/src/app.py\n+hello\n",
        pre_patch_git_hash="pre",
        post_patch_git_hash=git_hash,
        files_applied=["src/app.py"],
    )


def _test_result(run_id: str) -> PipelineTestResult:
    return PipelineTestResult(
        run_id=run_id,
        passed=True,
        output="1 passed",
        total_tests=1,
        passed_tests=1,
        failed_tests=0,
    )


def _fake_complete(text_value: str):
    async def _complete(role, request, overrides=None):
        return LLMResponse(
            text=text_value,
            provider="anthropic",
            model="claude-reviewer",
            input_tokens=21,
            output_tokens=43,
            finish_reason="stop",
        )

    return _complete


async def _run_review(monkeypatch, tracked_runs, *, chunk_number: int = 1):
    run_id = _seed_run_with_test_checkpoint(
        tracked_runs, "HASH-A", chunk_number=chunk_number
    )
    monkeypatch.setattr(reviewer, "complete_for_role", _fake_complete(VALID_REVIEW_JSON))

    result = await run_chunk_review(
        run_id=run_id,
        project_id=None,
        chunk=_chunk(chunk_number),
        code=_code(run_id),
        patch=_patch(run_id, "HASH-A"),
        test_result=_test_result(run_id),
    )
    return run_id, result


@pytest.mark.asyncio
async def test_reviewer_persists_provenance_with_identity_and_tokens(
    monkeypatch, tracked_runs
):
    chunk_number = 3
    run_id, result = await _run_review(
        monkeypatch, tracked_runs, chunk_number=chunk_number
    )

    assert result is not None
    assert result.review_status == ChunkReviewStatus.COMPLETED
    assert result.verdict == ChunkReviewVerdict.APPROVE_WITH_NOTES

    rows = get_provenance_for_chunk(run_id, chunk_number, role=Role.REVIEWER.value)
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == run_id
    assert row.chunk_number == chunk_number
    assert row.role == "reviewer"
    assert row.provider == "anthropic"
    assert row.model == "claude-reviewer"
    assert row.finish_reason == "stop"
    assert row.input_tokens == 21
    assert row.output_tokens == 43
    assert row.selection_source is None


@pytest.mark.asyncio
async def test_reviewer_provenance_failure_is_swallowed(monkeypatch, tracked_runs):
    def _boom(_record):
        raise RuntimeError("simulated provenance DB failure with token sk-test")

    monkeypatch.setattr(store, "record_llm_call_provenance", _boom)

    run_id, result = await _run_review(monkeypatch, tracked_runs)

    assert result is not None
    assert result.review_status == ChunkReviewStatus.COMPLETED
    assert get_provenance_for_chunk(run_id, 1, role=Role.REVIEWER.value) == []


@pytest.mark.asyncio
async def test_reviewer_provenance_schema_has_no_content_or_error_fields(
    monkeypatch, tracked_runs
):
    run_id, result = await _run_review(monkeypatch, tracked_runs)

    assert result is not None
    rows = get_provenance_for_chunk(run_id, 1, role=Role.REVIEWER.value)
    assert len(rows) == 1

    with engine.connect() as conn:
        table_info = conn.execute(
            text("PRAGMA table_info(llm_call_provenance)")
        ).fetchall()
        raw_row = conn.execute(
            text("SELECT * FROM llm_call_provenance WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).fetchone()

    column_names = {row._mapping["name"] for row in table_info}
    assert column_names == {
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
    forbidden_columns = {
        "prompt",
        "response",
        "text",
        "diff",
        "error",
        "raw_error",
        "secret",
        "token",
    }
    assert column_names.isdisjoint(forbidden_columns)
    persisted = dict(raw_row._mapping)
    assert persisted["role"] == "reviewer"
    persisted_text = " ".join(str(value) for value in persisted.values())
    assert "Looks reasonable" not in persisted_text
    assert "--- a/src/app.py" not in persisted_text
    assert "simulated provenance DB failure" not in persisted_text
