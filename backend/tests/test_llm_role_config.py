import pytest

from backend.config.keys import settings
from backend.llm.role_config import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    Role,
    resolve_role_config,
)

pytestmark = pytest.mark.unit


def _clear_settings(monkeypatch):
    for attr in (
        "default_llm_provider",
        "default_llm_model",
        "triage_llm_provider",
        "triage_llm_model",
        "planner_llm_provider",
        "planner_llm_model",
        "coder_llm_provider",
        "coder_llm_model",
        "reviewer_llm_provider",
        "reviewer_llm_model",
        "summary_llm_provider",
        "summary_llm_model",
        "architect_llm_provider",
        "architect_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)


def test_role_config_hardcoded_fallback(monkeypatch):
    _clear_settings(monkeypatch)
    monkeypatch.delenv("DEFAULT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_LLM_MODEL", raising=False)
    monkeypatch.delenv("PLANNER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)

    config = resolve_role_config(Role.PLANNER)

    assert config.provider == DEFAULT_PROVIDER
    assert config.model == DEFAULT_MODEL


def test_role_config_default_env_override(monkeypatch):
    _clear_settings(monkeypatch)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "fake")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "fake-model")

    config = resolve_role_config(Role.CODER)

    assert config.provider == "fake"
    assert config.model == "fake-model"


def test_role_config_role_env_override(monkeypatch):
    _clear_settings(monkeypatch)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv("CODER_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CODER_LLM_MODEL", "fake-model")

    config = resolve_role_config(Role.CODER)

    assert config.provider == "fake"
    assert config.model == "fake-model"


def test_role_config_explicit_overrides_win(monkeypatch):
    _clear_settings(monkeypatch)
    monkeypatch.setenv("PLANNER_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("PLANNER_LLM_MODEL", "gemini-2.5-flash-lite")

    config = resolve_role_config(
        Role.PLANNER,
        {
            "planner_provider": "fake",
            "planner_model": "fake-model",
        },
    )

    assert config.provider == "fake"
    assert config.model == "fake-model"
