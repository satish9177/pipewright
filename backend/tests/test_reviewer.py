"""
test_reviewer.py
Unit tests for the internal advisory reviewer (backend/pipeline/reviewer.py).

No real LLM, no real git: complete_for_role is monkeypatched and the chunk diff
identity is seeded via a real test checkpoint. These prove a completed review is
stored and bound to the existing identity, and that EVERY failure mode (provider
error, malformed JSON, invalid model output, storage error) is swallowed into an
unavailable record (or None) without raising.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.db.database import engine, init_db
from backend.llm.base import LLMResponse
from backend.models.chunk import ChunkDefinition
from backend.models.handoff import CoderHandoff, FileChange, PatchResult, PipelineTestResult
from backend.pipeline import reviewer
from backend.pipeline.chunk_review_store import get_latest_review, review_staleness
from backend.pipeline.reviewer import (
    REVIEWER_MAX_DIFF_CHARS,
    build_reviewer_prompt,
    parse_completed_review,
    run_chunk_review,
)
from backend.pipeline.reviewer_models import (
    ChunkReviewStatus,
    ChunkReviewVerdict,
    ReviewStalenessStatus,
)

pytestmark = pytest.mark.unit


VALID_REVIEW_JSON = json.dumps({
    "verdict": "approve_with_notes",
    "summary": "Looks reasonable; one note.",
    "findings": [
        {
            "category": "test_gap",
            "severity": "warning",
            "title": "New branch untested",
            "explanation": "No test exercises the added branch.",
            "affected_files": ["src/app.py"],
            "suggested_human_check": "Add a test for the new branch.",
            "confidence": 0.4,
        }
    ],
    "test_gap_summary": "One untested branch.",
    "scope_summary": "",
    "security_or_safety_summary": "",
    "recommended_human_action": "Review the new branch and add a test.",
})


@pytest.fixture()
def tracked_runs():
    init_db()
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in ("chunk_reviews", "checkpoints"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :r"), {"r": run_id}
                )


def _seed_run_with_test_checkpoint(tracked_runs, git_hash: str, chunk_number: int = 1) -> str:
    run_id = f"reviewer-test-{uuid.uuid4()}"
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


def _patch(run_id: str, git_hash: str, *, diff: str = "--- a/src/app.py\n+++ b/src/app.py\n+hello\n",
           files_applied=None) -> PatchResult:
    return PatchResult(
        run_id=run_id,
        success=True,
        diff=diff,
        pre_patch_git_hash="pre",
        post_patch_git_hash=git_hash,
        files_applied=files_applied if files_applied is not None else ["src/app.py"],
    )


def _test_result(run_id: str) -> PipelineTestResult:
    return PipelineTestResult(
        run_id=run_id, passed=True, output="1 passed", total_tests=1,
        passed_tests=1, failed_tests=0,
    )


def _fake_complete(text_value: str):
    async def _complete(role, request, overrides=None):
        return LLMResponse(text=text_value, provider="fakeprov", model="fakemodel")
    return _complete


# --- success path ------------------------------------------------------------

@pytest.mark.asyncio
async def test_completed_review_is_stored_and_bound_to_identity(monkeypatch, tracked_runs):
    run_id = _seed_run_with_test_checkpoint(tracked_runs, "HASH-A")
    monkeypatch.setattr(reviewer, "complete_for_role", _fake_complete(VALID_REVIEW_JSON))

    result = await run_chunk_review(
        run_id=run_id,
        project_id=None,
        chunk=_chunk(),
        code=_code(run_id),
        patch=_patch(run_id, "HASH-A"),
        test_result=_test_result(run_id),
    )

    assert result is not None
    assert result.review_status == ChunkReviewStatus.COMPLETED
    assert result.verdict == ChunkReviewVerdict.APPROVE_WITH_NOTES
    assert len(result.findings) == 1
    # Bound to the EXISTING test-checkpoint identity, not a new scheme.
    assert result.reviewed_test_checkpoint_hash == "HASH-A"
    assert result.provider == "fakeprov"
    assert result.model == "fakemodel"

    stored = get_latest_review(run_id, 1)
    assert stored is not None
    assert stored.id == result.id
    assert review_staleness(run_id, 1, "HASH-A") == ReviewStalenessStatus.CURRENT


# --- failure modes all swallow into unavailable, never raise -----------------

@pytest.mark.asyncio
async def test_malformed_json_stores_unavailable_and_does_not_raise(monkeypatch, tracked_runs):
    run_id = _seed_run_with_test_checkpoint(tracked_runs, "HASH-A")
    monkeypatch.setattr(reviewer, "complete_for_role", _fake_complete("this is not json"))

    result = await run_chunk_review(
        run_id=run_id, project_id=None, chunk=_chunk(), code=_code(run_id),
        patch=_patch(run_id, "HASH-A"), test_result=_test_result(run_id),
    )

    assert result is not None
    assert result.review_status == ChunkReviewStatus.UNAVAILABLE
    assert result.verdict is None
    assert result.findings == []
    assert get_latest_review(run_id, 1).review_status == ChunkReviewStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_invalid_model_output_stores_unavailable(monkeypatch, tracked_runs):
    run_id = _seed_run_with_test_checkpoint(tracked_runs, "HASH-A")
    bad = json.dumps({"verdict": "not_a_real_verdict", "summary": "x", "findings": []})
    monkeypatch.setattr(reviewer, "complete_for_role", _fake_complete(bad))

    result = await run_chunk_review(
        run_id=run_id, project_id=None, chunk=_chunk(), code=_code(run_id),
        patch=_patch(run_id, "HASH-A"), test_result=_test_result(run_id),
    )

    assert result.review_status == ChunkReviewStatus.UNAVAILABLE
    assert result.verdict is None


@pytest.mark.asyncio
async def test_provider_exception_stores_unavailable_and_does_not_raise(monkeypatch, tracked_runs):
    run_id = _seed_run_with_test_checkpoint(tracked_runs, "HASH-A")

    async def boom(role, request, overrides=None):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(reviewer, "complete_for_role", boom)

    result = await run_chunk_review(
        run_id=run_id, project_id=None, chunk=_chunk(), code=_code(run_id),
        patch=_patch(run_id, "HASH-A"), test_result=_test_result(run_id),
    )

    assert result is not None
    assert result.review_status == ChunkReviewStatus.UNAVAILABLE
    # The unavailable record is still bound to the existing identity.
    assert result.reviewed_test_checkpoint_hash == "HASH-A"


@pytest.mark.asyncio
async def test_storage_failure_is_swallowed_and_returns_none(monkeypatch, tracked_runs):
    run_id = _seed_run_with_test_checkpoint(tracked_runs, "HASH-A")

    async def boom(role, request, overrides=None):
        raise RuntimeError("provider exploded")

    def store_boom(record):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(reviewer, "complete_for_role", boom)
    monkeypatch.setattr(reviewer, "create_review", store_boom)

    # Even when the unavailable write also fails, run_chunk_review must not raise.
    result = await run_chunk_review(
        run_id=run_id, project_id=None, chunk=_chunk(), code=_code(run_id),
        patch=_patch(run_id, "HASH-A"), test_result=_test_result(run_id),
    )
    assert result is None


@pytest.mark.asyncio
async def test_indeterminate_identity_still_does_not_raise(monkeypatch, tracked_runs):
    # No test checkpoint => current_chunk_review_identity returns None.
    run_id = f"reviewer-test-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    monkeypatch.setattr(reviewer, "complete_for_role", _fake_complete(VALID_REVIEW_JSON))

    result = await run_chunk_review(
        run_id=run_id, project_id=None, chunk=_chunk(), code=_code(run_id),
        patch=_patch(run_id, "HASH-A"), test_result=_test_result(run_id),
    )
    assert result is not None
    assert result.review_status == ChunkReviewStatus.COMPLETED
    # No usable identity => stored hash is None; reads STALE, never falsely current.
    assert result.reviewed_test_checkpoint_hash is None
    assert review_staleness(run_id, 1, None) == ReviewStalenessStatus.STALE


# --- pure input shaping (no DB) ----------------------------------------------

def test_sanitize_diff_caps_head():
    big = "x" * (REVIEWER_MAX_DIFF_CHARS + 5000)
    patch = _patch("r", "h", diff=big, files_applied=["src/app.py"])
    out = reviewer._sanitize_diff(patch)
    assert len(out) < len(big)
    assert "truncated" in out


def test_sanitize_diff_omits_forbidden_path():
    patch = _patch("r", "h", diff="secret diff body", files_applied=[".env"])
    out = reviewer._sanitize_diff(patch)
    assert "diff omitted" in out
    assert "secret diff body" not in out


def test_cap_tail_keeps_tail():
    out = reviewer._cap_tail("HEAD" + "y" * 5000 + "TAIL-SUMMARY", 100)
    assert out.endswith("TAIL-SUMMARY")
    assert "truncated" in out


def test_build_prompt_is_bounded_and_grounded():
    run_id = "r"
    prompt = build_reviewer_prompt(
        run_id=run_id,
        chunk=_chunk(),
        code=_code(run_id),
        patch=_patch(run_id, "h", diff="d" * (REVIEWER_MAX_DIFF_CHARS + 1000)),
        test_result=_test_result(run_id),
        test_command="python -m pytest",
        verdict_block="Runtime test verdict: strong",
    )
    assert "Add a hello function" in prompt
    assert "python -m pytest" in prompt
    assert "Runtime test verdict: strong" in prompt
    # Diff is bounded inside the prompt.
    assert "truncated" in prompt


def test_parse_completed_review_valid():
    record = parse_completed_review(
        VALID_REVIEW_JSON,
        review_id="rid",
        run_id="run",
        chunk_number=1,
        reviewed_hash="HASH-A",
        provider="p",
        model="m",
    )
    assert record.review_status == ChunkReviewStatus.COMPLETED
    assert record.verdict == ChunkReviewVerdict.APPROVE_WITH_NOTES
    assert record.reviewed_test_checkpoint_hash == "HASH-A"


def test_parse_completed_review_invalid_raises():
    with pytest.raises(Exception):
        parse_completed_review(
            "garbage",
            review_id="rid",
            run_id="run",
            chunk_number=1,
            reviewed_hash=None,
            provider=None,
            model=None,
        )
