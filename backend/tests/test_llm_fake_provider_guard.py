"""
test_llm_fake_provider_guard.py
Tests for #33D: FakeProvider is registered but refused for real runtime
resolution unless explicitly allowed (pytest env or PIPEWRIGHT_ALLOW_FAKE_PROVIDER).

The guard lives at the single resolution chokepoint get_provider_for_role, so
complete_for_role and validate_all_roles are covered too. Direct registry
injection (provider.complete called directly) intentionally bypasses the guard
and is not exercised here.
"""

import pytest

from backend.llm import (
    LLMRequest,
    complete_for_role,
    get_provider_for_role,
    is_fake_provider_allowed,
)
from backend.llm.base import Message
from backend.llm.errors import ProviderConfigurationError
from backend.llm.providers.fake import FakeProvider
from backend.llm.role_config import Role, resolve_role_config

pytestmark = pytest.mark.unit


# --- pure helper -------------------------------------------------------------

def test_allowed_under_pytest_env():
    assert is_fake_provider_allowed({"PYTEST_CURRENT_TEST": "some::test"}) is True


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", " On "])
def test_allowed_for_explicit_truthy_flag(value):
    assert is_fake_provider_allowed({"PIPEWRIGHT_ALLOW_FAKE_PROVIDER": value}) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "maybe", "treu"])
def test_not_allowed_for_non_truthy_flag(value):
    assert is_fake_provider_allowed({"PIPEWRIGHT_ALLOW_FAKE_PROVIDER": value}) is False


def test_not_allowed_for_empty_env():
    assert is_fake_provider_allowed({}) is False


# --- integration at get_provider_for_role ------------------------------------

def _select_fake_for_coder(monkeypatch):
    monkeypatch.setenv("CODER_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CODER_LLM_MODEL", "fake-model")


def _disallow_fake_env(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PIPEWRIGHT_ALLOW_FAKE_PROVIDER", raising=False)


def test_fake_rejected_when_not_allowed(clear_llm_env, monkeypatch):
    _select_fake_for_coder(monkeypatch)
    _disallow_fake_env(monkeypatch)

    with pytest.raises(ProviderConfigurationError) as error:
        get_provider_for_role(Role.CODER)

    message = str(error.value)
    assert "Fake LLM provider is disabled outside tests" in message
    assert "PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true" in message
    # Sanitized: no env dump / secret-like content leaked into the message.
    assert "CODER_LLM_PROVIDER" not in message
    assert "os.environ" not in message


@pytest.mark.asyncio
async def test_fake_rejected_through_complete_for_role(clear_llm_env, monkeypatch):
    _select_fake_for_coder(monkeypatch)
    _disallow_fake_env(monkeypatch)

    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model="ignored",
    )
    with pytest.raises(ProviderConfigurationError):
        await complete_for_role(Role.CODER, request)


def test_fake_allowed_via_explicit_flag(clear_llm_env, monkeypatch):
    _select_fake_for_coder(monkeypatch)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PIPEWRIGHT_ALLOW_FAKE_PROVIDER", "true")

    provider, model = get_provider_for_role(Role.CODER)
    assert isinstance(provider, FakeProvider)
    assert model == "fake-model"


def test_fake_allowed_under_pytest_current_test(clear_llm_env, monkeypatch):
    _select_fake_for_coder(monkeypatch)
    monkeypatch.delenv("PIPEWRIGHT_ALLOW_FAKE_PROVIDER", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")

    provider, model = get_provider_for_role(Role.CODER)
    assert isinstance(provider, FakeProvider)
    assert model == "fake-model"


def test_non_fake_provider_unaffected(clear_llm_env, monkeypatch):
    # Even with fake disallowed, a normal provider resolves unchanged.
    _disallow_fake_env(monkeypatch)
    monkeypatch.setenv("CODER_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CODER_LLM_MODEL", "gemini-2.5-flash-lite")

    provider, model = get_provider_for_role(Role.CODER)
    assert provider.name == "gemini"
    assert model == "gemini-2.5-flash-lite"


def test_routing_precedence_unchanged(clear_llm_env, monkeypatch):
    # The guard does not touch resolve_role_config precedence: a role-specific var
    # still overrides the default, and the resolved provider name is unchanged.
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv("CODER_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CODER_LLM_MODEL", "claude-x")

    coder = resolve_role_config(Role.CODER)
    planner = resolve_role_config(Role.PLANNER)
    assert coder.provider == "anthropic"  # role var wins
    assert planner.provider == "gemini"   # falls back to default
