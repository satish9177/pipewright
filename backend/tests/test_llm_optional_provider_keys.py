"""
test_llm_optional_provider_keys.py

Provider API keys are optional at startup. A key is required only when its
provider is actually selected for a role that is validated. Notably,
GEMINI_API_KEY must NOT be required when no role resolves to Gemini.
"""

import pytest

from backend.config.keys import Settings, settings
from backend.llm import validate_all_roles
from backend.llm.errors import ProviderConfigurationError

pytestmark = pytest.mark.unit


_ALL_PROVIDER_KEYS = (
    "gemini_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "deepseek_api_key",
)

_ROLE_SETTINGS = (
    "default_llm_provider", "default_llm_model",
    "triage_llm_provider", "triage_llm_model",
    "planner_llm_provider", "planner_llm_model",
    "coder_llm_provider", "coder_llm_model",
    "reviewer_llm_provider", "reviewer_llm_model",
    "summary_llm_provider", "summary_llm_model",
    "architect_llm_provider", "architect_llm_model",
)

_ROLE_ENV = (
    "DEFAULT_LLM_PROVIDER", "DEFAULT_LLM_MODEL",
    "TRIAGE_LLM_PROVIDER", "TRIAGE_LLM_MODEL",
    "PLANNER_LLM_PROVIDER", "PLANNER_LLM_MODEL",
    "CODER_LLM_PROVIDER", "CODER_LLM_MODEL",
    "REVIEWER_LLM_PROVIDER", "REVIEWER_LLM_MODEL",
    "SUMMARY_LLM_PROVIDER", "SUMMARY_LLM_MODEL",
    "ARCHITECT_LLM_PROVIDER", "ARCHITECT_LLM_MODEL",
)


def _blank_slate(monkeypatch):
    """Clear all role config + all provider keys (settings attrs and env)."""
    for attr in _ROLE_SETTINGS:
        monkeypatch.setattr(settings, attr, None)
    for key in _ALL_PROVIDER_KEYS:
        monkeypatch.setattr(settings, key, None)
    for env in _ROLE_ENV:
        monkeypatch.delenv(env, raising=False)


# ─── settings import optionality ──────────────────────────────────────────────

def test_settings_does_not_require_gemini_key(monkeypatch):
    """Settings must construct without GEMINI_API_KEY set (no startup failure)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # _env_file=None prevents the repo .env from supplying the key.
    fresh = Settings(_env_file=None)
    assert fresh.gemini_api_key is None


# ─── Anthropic-only config (example 1) ────────────────────────────────────────

def test_all_roles_anthropic_requires_only_anthropic_key(monkeypatch):
    _blank_slate(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "default_llm_model", "claude-sonnet-4-5")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    # GEMINI_API_KEY intentionally left None.

    validate_all_roles()  # must not raise


def test_all_roles_anthropic_missing_anthropic_key_raises(monkeypatch):
    _blank_slate(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "default_llm_model", "claude-sonnet-4-5")
    # No anthropic key, no gemini key.

    with pytest.raises(ProviderConfigurationError) as exc_info:
        validate_all_roles()

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    # Failure is about the selected provider, not the absent Gemini key.
    assert "GEMINI_API_KEY" not in message


# ─── mixed Anthropic planner + OpenAI coder (example 2) ───────────────────────

def test_mixed_anthropic_openai_requires_both_selected_keys(monkeypatch):
    _blank_slate(monkeypatch)
    # Default anthropic so no role falls back to Gemini.
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "default_llm_model", "claude-sonnet-4-5")
    monkeypatch.setattr(settings, "coder_llm_provider", "openai")
    monkeypatch.setattr(settings, "coder_llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai-test")

    validate_all_roles()  # both keys present -> passes, no Gemini needed


def test_mixed_config_missing_openai_key_raises(monkeypatch):
    _blank_slate(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "default_llm_model", "claude-sonnet-4-5")
    monkeypatch.setattr(settings, "coder_llm_provider", "openai")
    monkeypatch.setattr(settings, "coder_llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    # openai key missing

    with pytest.raises(ProviderConfigurationError) as exc_info:
        validate_all_roles()

    assert "OPENAI_API_KEY" in str(exc_info.value)


# ─── default fallback Gemini (example 3) ──────────────────────────────────────

def test_default_fallback_gemini_requires_gemini_key(monkeypatch):
    """With no provider config, the Gemini fallback means GEMINI_API_KEY is needed."""
    _blank_slate(monkeypatch)
    # No provider/model config at all -> roles resolve to gemini fallback.

    with pytest.raises(ProviderConfigurationError) as exc_info:
        validate_all_roles()

    assert "GEMINI_API_KEY" in str(exc_info.value)


def test_default_fallback_gemini_passes_with_gemini_key(monkeypatch):
    _blank_slate(monkeypatch)
    monkeypatch.setattr(settings, "gemini_api_key", "AIzaSyA123456789012345678901234567890")

    validate_all_roles()  # must not raise


# ─── fake provider needs no real keys (example 4) ─────────────────────────────

def test_fake_provider_needs_no_keys(monkeypatch):
    _blank_slate(monkeypatch)
    monkeypatch.setattr(settings, "default_llm_provider", "fake")
    monkeypatch.setattr(settings, "default_llm_model", "fake-model")
    # All provider keys are None.

    validate_all_roles()  # must not raise
