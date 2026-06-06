"""
test_llm_logging.py
Tests for consistent provider/model/token audit logging via log_token_usage.

Covers:
  - Shared helper behaviour (fields, None tokens, no prompt/raw leakage)
  - Per-role integration (triage, planner, coder call the shared helper)
  - Guard: log lines produced by the shared helper never contain prompt text
"""

import json
import logging
import uuid
from types import SimpleNamespace

import pytest


def _empty_memory_result(**kwargs):
    return SimpleNamespace(
        block="", role=kwargs.get("role"), token_budget=0,
        category_policy=(), included_entries=(), excluded_entries=(),
    )

from backend.llm import log_token_usage
from backend.llm.base import LLMResponse
from backend.llm.role_config import Role
from backend.models.handoff import PlannerHandoff
from backend.pipeline import coder, planner, triage

pytestmark = pytest.mark.unit


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _enable_backend_log_propagation():
    """
    configure_logging() sets backend.propagate = False so its StreamHandler
    receives records instead of forwarding them to the root logger.
    pytest caplog installs its handler on the root logger, so it misses any
    records when propagation is disabled. Temporarily re-enable propagation for
    the duration of each logging test so caplog.records is populated correctly.
    """
    backend_logger = logging.getLogger("backend")
    original = backend_logger.propagate
    backend_logger.propagate = True
    yield
    backend_logger.propagate = original


# ─── helpers ─────────────────────────────────────────────────────────────────

def _resp(**kwargs) -> LLMResponse:
    defaults = dict(
        text="response text",
        provider="fake",
        model="fake-model",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
    )
    return LLMResponse(**{**defaults, **kwargs})


def _planner_json(run_id: str) -> str:
    return json.dumps({
        "handoff_from": "planner", "handoff_to": "coder",
        "run_id": run_id, "feature_description": "Add ping",
        "goal": "Ping endpoint.", "steps": ["Add route"],
        "files_to_create": [], "files_to_modify": [],
        "files_to_read": [], "out_of_scope": [],
        "risks": [], "suggested_memory_entries": [],
    })


def _coder_json(run_id: str) -> str:
    return json.dumps({
        "handoff_from": "coder", "handoff_to": "patch_applier",
        "run_id": run_id, "feature_description": "Add ping",
        "files_changed": [], "summary": "Done.",
        "suggested_memory_entries": [],
    })


def _triage_json(run_id: str, project_id: str) -> str:
    return json.dumps({
        "run_id": run_id, "project_id": project_id,
        "feature_description": "Add ping", "complexity": "easy",
        "total_chunks": 1, "reasoning": "Simple.",
        "chunks": [{
            "chunk_number": 1, "title": "Add route",
            "description": "Ping route.", "files_expected": [],
            "depends_on": [], "risk_level": "low",
            "token_estimate": 500, "requires_human_review": False,
            "rationale": "Isolated.",
        }],
    })


# ─── log_token_usage helper ───────────────────────────────────────────────────

def test_log_token_usage_includes_provider_model_role(caplog):
    with caplog.at_level(logging.INFO, logger="backend.llm"):
        log_token_usage(_resp(input_tokens=10, output_tokens=5), run_id="run-1", role=Role.PLANNER)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "role=planner" in msg
    assert "provider=fake" in msg
    assert "model=fake-model" in msg
    assert "input_tokens=10" in msg
    assert "output_tokens=5" in msg
    assert "run_id=run-1" in msg


def test_log_token_usage_includes_finish_reason(caplog):
    with caplog.at_level(logging.INFO, logger="backend.llm"):
        log_token_usage(_resp(finish_reason="stop"), run_id="run-1", role=Role.TRIAGE)

    assert "finish_reason=stop" in caplog.records[0].getMessage()


def test_log_token_usage_handles_missing_tokens(caplog):
    with caplog.at_level(logging.INFO, logger="backend.llm"):
        log_token_usage(_resp(input_tokens=None, output_tokens=None), run_id="run-1", role=Role.TRIAGE)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "unavailable" in msg


def test_log_token_usage_handles_none_finish_reason(caplog):
    with caplog.at_level(logging.INFO, logger="backend.llm"):
        log_token_usage(_resp(finish_reason=None), run_id="run-1", role=Role.CODER)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "finish_reason=unknown" in msg


def test_log_token_usage_handles_missing_run_id(caplog):
    with caplog.at_level(logging.INFO, logger="backend.llm"):
        log_token_usage(_resp(), role=Role.CODER)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "run_id=none" in msg


def test_log_token_usage_does_not_log_prompt_or_raw(caplog):
    sentinel = "SECRET_PROMPT_SENTINEL_DO_NOT_LOG_XYZ987"
    response = _resp(
        text=sentinel,
        raw={"api_key": sentinel, "prompt": sentinel},
    )

    with caplog.at_level(logging.DEBUG, logger="backend.llm"):
        log_token_usage(response, run_id="run-1", role=Role.PLANNER)

    full_log = " ".join(r.getMessage() for r in caplog.records)
    assert sentinel not in full_log


# ─── pipeline role integration ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_triage_logs_provider_model(monkeypatch, caplog, tmp_repo):
    run_id = str(uuid.uuid4())
    project_id = "proj-log-triage"

    monkeypatch.setattr(
        triage, "require_project",
        lambda pid: {"id": pid, "name": "Test", "repo_path": str(tmp_repo)},
    )
    monkeypatch.setattr(triage, "ensure_repo_indexed", lambda pid, rp: {})
    monkeypatch.setattr(triage, "get_relevant_files", lambda pid, q, limit=20: [])

    async def _fake_complete(role, request, overrides=None):
        return LLMResponse(
            text=_triage_json(run_id, project_id),
            provider="fake",
            model="fake-model",
            input_tokens=10,
            output_tokens=5,
            finish_reason="stop",
        )

    monkeypatch.setattr(triage, "complete_for_role", _fake_complete)

    with caplog.at_level(logging.INFO, logger="backend.llm"):
        await triage.run_triage(run_id, project_id, "Add ping")

    full_log = " ".join(r.getMessage() for r in caplog.records)
    assert "role=triage" in full_log
    assert "provider=fake" in full_log
    assert "model=fake-model" in full_log
    assert "input_tokens=10" in full_log


@pytest.mark.asyncio
async def test_planner_logs_provider_model(monkeypatch, caplog):
    run_id = str(uuid.uuid4())

    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(planner, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)

    async def _fake_complete(role, request, overrides=None):
        return LLMResponse(
            text=_planner_json(run_id),
            provider="fake",
            model="fake-model",
            input_tokens=15,
            output_tokens=8,
            finish_reason="stop",
        )

    monkeypatch.setattr(planner, "complete_for_role", _fake_complete)

    with caplog.at_level(logging.INFO, logger="backend.llm"):
        await planner.run_planner("Add ping", run_id)

    full_log = " ".join(r.getMessage() for r in caplog.records)
    assert "role=planner" in full_log
    assert "provider=fake" in full_log
    assert "model=fake-model" in full_log
    assert "input_tokens=15" in full_log


@pytest.mark.asyncio
async def test_coder_logs_provider_model(monkeypatch, caplog, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = PlannerHandoff(
        run_id=run_id,
        feature_description="Add ping",
        goal="Ping endpoint.",
        steps=["Add route"],
        files_to_create=[], files_to_modify=[], files_to_read=[],
        out_of_scope=[], risks=[], suggested_memory_entries=[],
    )

    monkeypatch.setattr(
        coder, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(coder, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(coder, "get_target_repo_path", lambda: str(tmp_repo))
    monkeypatch.setattr(coder, "save_checkpoint", lambda **kwargs: None)

    async def _fake_complete(role, request, overrides=None):
        return LLMResponse(
            text=_coder_json(run_id),
            provider="fake",
            model="fake-model",
            input_tokens=20,
            output_tokens=10,
            finish_reason="stop",
        )

    monkeypatch.setattr(coder, "complete_for_role", _fake_complete)

    with caplog.at_level(logging.INFO, logger="backend.llm"):
        await coder.run_coder(plan=plan, run_id=run_id)

    full_log = " ".join(r.getMessage() for r in caplog.records)
    assert "role=coder" in full_log
    assert "provider=fake" in full_log
    assert "model=fake-model" in full_log
    assert "input_tokens=20" in full_log


def test_provider_model_logs_do_not_include_prompt_content(caplog):
    """log_token_usage must never emit response text or raw metadata."""
    unique_sentinel = "UNIQUE_PROMPT_CONTENT_SENTINEL_DO_NOT_LOG_99999"
    response = _resp(
        text=unique_sentinel,
        raw={"prompt_echo": unique_sentinel, "full_response": unique_sentinel},
    )

    with caplog.at_level(logging.DEBUG, logger="backend.llm"):
        log_token_usage(response, run_id="run-2", role=Role.PLANNER)

    full_log = " ".join(r.getMessage() for r in caplog.records)
    assert unique_sentinel not in full_log
