"""
test_coder_provenance.py
Integration tests for #33B: the coder persists METADATA-ONLY provenance for its
effective output, and a provenance-write failure never affects the coder result.

Fully mocked: no real LLM, no git, no checkpoint, no real test execution. The
coder's LLM provider and side-effecting deps are monkeypatched, exactly like
test_coder.py.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.llm.base import LLMResponse
from backend.llm.role_config import Role
from backend.models.handoff import PlannerHandoff
from backend.pipeline import coder
from backend.pipeline import llm_call_provenance_store as store
from backend.pipeline.coder import run_coder
from backend.pipeline.llm_call_provenance_store import get_provenance_for_chunk

pytestmark = pytest.mark.unit


def _make_plan(run_id: str) -> PlannerHandoff:
    return PlannerHandoff(
        run_id=run_id,
        feature_description="Add a ping endpoint",
        goal="Create a ping endpoint",
        steps=["Create a ping route", "Return ok"],
        files_to_create=["backend/routes/ping.py"],
        files_to_modify=[],
        files_to_read=[],
        out_of_scope=["auth"],
        risks=[],
        suggested_memory_entries=[],
    )


def _coder_json(run_id: str) -> str:
    return (
        "{"
        '"handoff_from": "coder",'
        '"handoff_to": "patch_applier",'
        f'"run_id": "{run_id}",'
        '"feature_description": "Add a ping endpoint",'
        '"files_changed": [{'
        '"path": "backend/routes/ping.py",'
        '"action": "create",'
        '"content": "def ping():\\n    return {\'status\': \'ok\'}\\n",'
        '"reason": "Add ping route"'
        "}],"
        '"summary": "Added a ping route.",'
        '"suggested_memory_entries": []'
        "}"
    )


class _FakeLLM:
    """Stands in for complete_for_role(role, request)."""

    def __init__(self, text_out: str, *, provider="anthropic", model="claude-x",
                 input_tokens=12, output_tokens=34, finish_reason="stop"):
        self.text_out = text_out
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.finish_reason = finish_reason

    async def complete(self, role, request):
        return LLMResponse(
            text=self.text_out,
            provider=self.provider,
            model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            finish_reason=self.finish_reason,
        )


def _patch_coder(monkeypatch, llm, tmp_repo):
    monkeypatch.setattr(coder, "build_project_memory_block", lambda **kwargs: "")
    monkeypatch.setattr(coder, "get_target_repo_path", lambda: str(tmp_repo))
    monkeypatch.setattr(coder, "save_checkpoint", lambda **kwargs: None)
    monkeypatch.setattr(coder, "complete_for_role", llm.complete)


def _cleanup(run_id: str):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM llm_call_provenance WHERE run_id = :run_id"),
            {"run_id": run_id},
        )


@pytest.mark.asyncio
async def test_coder_persists_provenance_with_identity_and_tokens(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    chunk_number = 4
    llm = _FakeLLM(_coder_json(run_id))
    _patch_coder(monkeypatch, llm, tmp_repo)

    try:
        result = await run_coder(
            plan=_make_plan(run_id), run_id=run_id, chunk_number=chunk_number
        )
        assert result.run_id == run_id

        rows = get_provenance_for_chunk(run_id, chunk_number, role=Role.CODER.value)
        assert len(rows) == 1
        row = rows[0]
        assert row.run_id == run_id
        assert row.chunk_number == chunk_number
        assert row.role == "coder"
        # The ACTUAL provider/model that produced the output (from the response).
        assert row.provider == "anthropic"
        assert row.model == "claude-x"
        # Token counts / finish_reason persisted when available.
        assert row.input_tokens == 12
        assert row.output_tokens == 34
        assert row.finish_reason == "stop"
        # selection_source is reserved/not derived in #33B.
        assert row.selection_source is None
    finally:
        _cleanup(run_id)


@pytest.mark.asyncio
async def test_coder_persists_exactly_one_row_even_with_retry(monkeypatch, tmp_repo):
    """A correction retry must not multiply provenance rows: one effective output."""
    run_id = str(uuid.uuid4())
    chunk_number = 1

    class _RetryLLM:
        def __init__(self):
            self.calls = 0

        async def complete(self, role, request):
            self.calls += 1
            text_out = "{not json" if self.calls == 1 else _coder_json(run_id)
            return LLMResponse(
                text=text_out,
                provider="gemini",
                model="gemini-2.5-flash-lite",
                input_tokens=1,
                output_tokens=2,
                finish_reason="stop",
            )

    llm = _RetryLLM()
    _patch_coder(monkeypatch, llm, tmp_repo)

    try:
        await run_coder(plan=_make_plan(run_id), run_id=run_id, chunk_number=chunk_number)
        assert llm.calls == 2  # first call failed to parse, retry succeeded
        rows = get_provenance_for_chunk(run_id, chunk_number, role=Role.CODER.value)
        assert len(rows) == 1  # only the effective output is recorded
        assert rows[0].provider == "gemini"
    finally:
        _cleanup(run_id)


@pytest.mark.asyncio
async def test_provenance_failure_does_not_fail_coder(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    chunk_number = 2
    llm = _FakeLLM(_coder_json(run_id))
    _patch_coder(monkeypatch, llm, tmp_repo)

    # Make the strict writer blow up. The coder uses the best-effort wrapper, which
    # must swallow this so the coder result is unchanged.
    def _boom(_record):
        raise RuntimeError("simulated provenance DB failure")

    monkeypatch.setattr(store, "record_llm_call_provenance", _boom)

    try:
        result = await run_coder(
            plan=_make_plan(run_id), run_id=run_id, chunk_number=chunk_number
        )
        # Coder still produced a valid handoff.
        assert result.run_id == run_id
        assert len(result.files_changed) == 1
        # And nothing was persisted.
        assert get_provenance_for_chunk(run_id, chunk_number) == []
    finally:
        _cleanup(run_id)
