"""
test_llm_role_config_pipeline.py
Integration tests verifying that DEFAULT_LLM_PROVIDER/MODEL and per-role
env overrides correctly reach the migrated pipeline roles at runtime.

All tests are @pytest.mark.unit — no live API calls, no Gemini key required.

Strategy: patch complete_for_role in each pipeline module with a spy that
calls the real get_provider_for_role (which reads env/settings) and asserts
the resolved provider matches the expected one, then returns canned JSON so
the role function can complete normally.
"""

import json
import uuid
from types import SimpleNamespace

import pytest


def _empty_memory_result(**kwargs):
    return SimpleNamespace(
        block="", role=kwargs.get("role"), token_budget=0,
        category_policy=(), included_entries=(), excluded_entries=(),
    )

from backend.config.keys import settings
from backend.llm import get_provider_for_role, validate_all_roles
from backend.llm.base import LLMResponse
from backend.llm.errors import UnsupportedModelError, UnsupportedProviderError
from backend.llm.role_config import DEFAULT_MODEL, DEFAULT_PROVIDER, Role, resolve_role_config
from backend.models.handoff import PlannerHandoff
from backend.pipeline import coder, planner, triage

pytestmark = pytest.mark.unit


# ─── shared helpers ───────────────────────────────────────────────────────────

def _clear_llm_settings(monkeypatch):
    """Reset all LLM provider/model settings and env vars to a blank slate."""
    for attr in (
        "default_llm_provider", "default_llm_model",
        "triage_llm_provider", "triage_llm_model",
        "planner_llm_provider", "planner_llm_model",
        "coder_llm_provider", "coder_llm_model",
        "reviewer_llm_provider", "reviewer_llm_model",
        "summary_llm_provider", "summary_llm_model",
        "architect_llm_provider", "architect_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    for env in (
        "DEFAULT_LLM_PROVIDER", "DEFAULT_LLM_MODEL",
        "TRIAGE_LLM_PROVIDER", "TRIAGE_LLM_MODEL",
        "PLANNER_LLM_PROVIDER", "PLANNER_LLM_MODEL",
        "CODER_LLM_PROVIDER", "CODER_LLM_MODEL",
    ):
        monkeypatch.delenv(env, raising=False)


def _routing_spy(expected_provider: str, response_text: str):
    """
    Returns a complete_for_role replacement that:
    1. Calls real get_provider_for_role to resolve provider from current env/settings.
    2. Asserts the resolved provider name equals expected_provider.
    3. Returns response_text as LLMResponse (no live API call).
    """
    async def _spy(role, request, overrides=None):
        provider, model = get_provider_for_role(role, overrides)
        assert provider.name == expected_provider, (
            f"role={role.value}: expected provider {expected_provider!r}, "
            f"got {provider.name!r}"
        )
        return LLMResponse(text=response_text, provider=provider.name, model=model)
    return _spy


# ─── JSON fixtures ────────────────────────────────────────────────────────────

def _triage_json(run_id: str, project_id: str) -> str:
    return json.dumps({
        "run_id": run_id,
        "project_id": project_id,
        "feature_description": "Add ping endpoint",
        "complexity": "easy",
        "total_chunks": 1,
        "reasoning": "Simple.",
        "chunks": [{
            "chunk_number": 1,
            "title": "Add route",
            "description": "Add ping route.",
            "files_expected": ["backend/routes/ping.py"],
            "depends_on": [],
            "risk_level": "low",
            "token_estimate": 500,
            "requires_human_review": False,
            "rationale": "Isolated change.",
        }],
    })


def _planner_json(run_id: str) -> str:
    return json.dumps({
        "handoff_from": "planner",
        "handoff_to": "coder",
        "run_id": run_id,
        "feature_description": "Add ping endpoint",
        "goal": "Create ping endpoint.",
        "steps": ["Add route", "Return response"],
        "files_to_create": [],
        "files_to_modify": ["backend/main.py"],
        "files_to_read": [],
        "out_of_scope": [],
        "risks": [],
        "suggested_memory_entries": [],
    })


def _coder_json(run_id: str) -> str:
    return json.dumps({
        "handoff_from": "coder",
        "handoff_to": "patch_applier",
        "run_id": run_id,
        "feature_description": "Add ping endpoint",
        "files_changed": [],
        "summary": "Added ping endpoint.",
        "suggested_memory_entries": [],
    })


def _make_test_plan(run_id: str) -> PlannerHandoff:
    return PlannerHandoff(
        run_id=run_id,
        feature_description="Add a GET /ping endpoint",
        goal="Create a simple ping endpoint",
        steps=["Add route", "Return pong"],
        files_to_create=[],
        files_to_modify=["backend/main.py"],
        files_to_read=[],
        out_of_scope=[],
        risks=[],
        suggested_memory_entries=[],
    )


# ─── infra patches ────────────────────────────────────────────────────────────

def _patch_triage_infra(monkeypatch, tmp_repo):
    monkeypatch.setattr(
        triage, "require_project",
        lambda project_id: {"id": project_id, "name": "Test", "repo_path": str(tmp_repo)},
    )
    monkeypatch.setattr(
        triage, "ensure_repo_indexed",
        lambda project_id, repo_path: {"status": "already_indexed"},
    )
    monkeypatch.setattr(
        triage, "get_relevant_files",
        lambda project_id, query, limit=20: [],
    )


def _patch_planner_infra(monkeypatch):
    monkeypatch.setattr(
        planner, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(planner, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(planner, "save_checkpoint", lambda **kwargs: None)


def _patch_coder_infra(monkeypatch, tmp_repo):
    monkeypatch.setattr(
        coder, "build_project_memory_block_detailed", _empty_memory_result
    )
    monkeypatch.setattr(coder, "capture_memory_injection", lambda *a, **k: None)
    monkeypatch.setattr(coder, "get_target_repo_path", lambda: str(tmp_repo))
    monkeypatch.setattr(coder, "save_checkpoint", lambda **kwargs: None)


# ─── default env routing ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_provider_env_routes_triage_to_fake(monkeypatch, tmp_repo):
    run_id, project_id = str(uuid.uuid4()), "proj-fake-triage"
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "fake")
    monkeypatch.setattr(settings, "default_llm_model", "fake-model")
    _patch_triage_infra(monkeypatch, tmp_repo)
    monkeypatch.setattr(
        triage, "complete_for_role",
        _routing_spy("fake", _triage_json(run_id, project_id)),
    )

    result = await triage.run_triage(run_id, project_id, "Add ping endpoint")

    assert result.total_chunks == 1


@pytest.mark.asyncio
async def test_default_provider_env_routes_planner_to_fake(monkeypatch):
    run_id = str(uuid.uuid4())
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "fake")
    monkeypatch.setattr(settings, "default_llm_model", "fake-model")
    _patch_planner_infra(monkeypatch)
    monkeypatch.setattr(
        planner, "complete_for_role",
        _routing_spy("fake", _planner_json(run_id)),
    )

    result = await planner.run_planner("Add ping endpoint", run_id)

    assert result.handoff_from == "planner"
    assert result.run_id == run_id


@pytest.mark.asyncio
async def test_default_provider_env_routes_coder_to_fake(monkeypatch, tmp_repo):
    run_id = str(uuid.uuid4())
    plan = _make_test_plan(run_id)
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "fake")
    monkeypatch.setattr(settings, "default_llm_model", "fake-model")
    _patch_coder_infra(monkeypatch, tmp_repo)
    monkeypatch.setattr(
        coder, "complete_for_role",
        _routing_spy("fake", _coder_json(run_id)),
    )

    result = await coder.run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"
    assert result.run_id == run_id


# ─── role-specific override ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_role_override_wins_for_triage(monkeypatch, tmp_repo):
    """TRIAGE_LLM_PROVIDER=fake wins over DEFAULT_LLM_PROVIDER=gemini."""
    run_id, project_id = str(uuid.uuid4()), "proj-triage-override"
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "gemini")
    monkeypatch.setattr(settings, "default_llm_model", "gemini-2.5-flash-lite")
    monkeypatch.setattr(settings, "triage_llm_provider", "fake")
    monkeypatch.setattr(settings, "triage_llm_model", "fake-model")
    _patch_triage_infra(monkeypatch, tmp_repo)
    monkeypatch.setattr(
        triage, "complete_for_role",
        _routing_spy("fake", _triage_json(run_id, project_id)),
    )

    result = await triage.run_triage(run_id, project_id, "Add ping endpoint")

    assert result.total_chunks == 1


@pytest.mark.asyncio
async def test_role_override_wins_for_planner(monkeypatch):
    """PLANNER_LLM_PROVIDER=fake wins over DEFAULT_LLM_PROVIDER=gemini."""
    run_id = str(uuid.uuid4())
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "gemini")
    monkeypatch.setattr(settings, "default_llm_model", "gemini-2.5-flash-lite")
    monkeypatch.setattr(settings, "planner_llm_provider", "fake")
    monkeypatch.setattr(settings, "planner_llm_model", "fake-model")
    _patch_planner_infra(monkeypatch)
    monkeypatch.setattr(
        planner, "complete_for_role",
        _routing_spy("fake", _planner_json(run_id)),
    )

    result = await planner.run_planner("Add ping endpoint", run_id)

    assert result.handoff_from == "planner"


@pytest.mark.asyncio
async def test_role_override_wins_for_coder(monkeypatch, tmp_repo):
    """CODER_LLM_PROVIDER=fake wins over DEFAULT_LLM_PROVIDER=gemini."""
    run_id = str(uuid.uuid4())
    plan = _make_test_plan(run_id)
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "gemini")
    monkeypatch.setattr(settings, "default_llm_model", "gemini-2.5-flash-lite")
    monkeypatch.setattr(settings, "coder_llm_provider", "fake")
    monkeypatch.setattr(settings, "coder_llm_model", "fake-model")
    _patch_coder_infra(monkeypatch, tmp_repo)
    monkeypatch.setattr(
        coder, "complete_for_role",
        _routing_spy("fake", _coder_json(run_id)),
    )

    result = await coder.run_coder(plan=plan, run_id=run_id)

    assert result.handoff_from == "coder"


# ─── cross-role isolation ─────────────────────────────────────────────────────

def test_role_override_does_not_leak_to_other_roles(monkeypatch):
    """A TRIAGE-specific override must not affect planner or coder resolution."""
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "triage_llm_provider", "fake")
    monkeypatch.setattr(settings, "triage_llm_model", "fake-model")

    triage_config = resolve_role_config(Role.TRIAGE)
    planner_config = resolve_role_config(Role.PLANNER)
    coder_config = resolve_role_config(Role.CODER)

    assert triage_config.provider == "fake"
    assert triage_config.model == "fake-model"
    assert planner_config.provider == DEFAULT_PROVIDER
    assert planner_config.model == DEFAULT_MODEL
    assert coder_config.provider == DEFAULT_PROVIDER
    assert coder_config.model == DEFAULT_MODEL


# ─── validation error tests ───────────────────────────────────────────────────

def test_unknown_provider_validation_fails_safely(monkeypatch):
    """An unknown provider name raises UnsupportedProviderError without leaking secrets."""
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "does-not-exist")
    monkeypatch.setattr(settings, "default_llm_model", "some-model")

    with pytest.raises(UnsupportedProviderError) as exc_info:
        get_provider_for_role(Role.TRIAGE)

    error_str = str(exc_info.value)
    assert "does-not-exist" in error_str
    assert "AIzaSy" not in error_str


def test_unsupported_model_validation_fails_safely(monkeypatch):
    """A model unsupported by the selected provider raises UnsupportedModelError."""
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "fake")
    monkeypatch.setattr(settings, "default_llm_model", "not-a-real-model")

    with pytest.raises(UnsupportedModelError):
        get_provider_for_role(Role.TRIAGE)


# ─── Gemini key isolation ─────────────────────────────────────────────────────

def test_fake_provider_allows_pipeline_tests_without_gemini_key(monkeypatch):
    """
    Selecting FakeProvider must not trigger any Gemini API key lookup.
    Simulating a missing GEMINI_API_KEY must not cause ProviderConfigurationError
    as long as no Gemini path is invoked.
    """
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "fake")
    monkeypatch.setattr(settings, "default_llm_model", "fake-model")
    monkeypatch.setattr(settings, "gemini_api_key", "")

    provider, model = get_provider_for_role(Role.TRIAGE)

    assert provider.name == "fake"
    assert model == "fake-model"


# ─── no-env default fallback ──────────────────────────────────────────────────

def test_no_env_vars_resolves_to_gemini_default(monkeypatch):
    """Without any LLM env vars, all pipeline roles fall back to the Gemini default."""
    _clear_llm_settings(monkeypatch)

    for role in (Role.TRIAGE, Role.PLANNER, Role.CODER):
        config = resolve_role_config(role)
        assert config.provider == DEFAULT_PROVIDER
        assert config.model == DEFAULT_MODEL


def test_explicit_overrides_dict_wins_over_env(monkeypatch):
    """resolve_role_config overrides dict takes precedence over env/settings."""
    _clear_llm_settings(monkeypatch)
    monkeypatch.setattr(settings, "triage_llm_provider", "gemini")
    monkeypatch.setattr(settings, "triage_llm_model", "gemini-2.5-flash-lite")

    config = resolve_role_config(
        Role.TRIAGE,
        {"triage_provider": "fake", "triage_model": "fake-model"},
    )

    assert config.provider == "fake"
    assert config.model == "fake-model"
