"""
test_llm_anthropic_provider.py
Unit tests for AnthropicProvider. All SDK calls are fully mocked — no live
API calls, no ANTHROPIC_API_KEY required.
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic

from backend.config.keys import settings
from backend.llm.base import LLMRequest, LLMResponse, Message
from backend.llm.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from backend.llm.providers.anthropic import AnthropicProvider, _translate_messages
from backend.llm.registry import default_registry
from backend.llm.role_config import Role, resolve_role_config

pytestmark = pytest.mark.unit

# ─── helpers ─────────────────────────────────────────────────────────────────

_FAKE_KEY = "sk-ant-test-fakekey1234567890abcdef"
_MODEL = "claude-3-5-haiku-latest"


def _make_httpx_response(status_code: int) -> httpx.Response:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status_code, request=req, text="{}")


def _sdk_error(cls, message: str, status_code: int) -> Exception:
    return cls(message, response=_make_httpx_response(status_code), body={})


def _mock_response(
    text: str = "ok",
    input_tokens: int = 10,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    resp.stop_reason = stop_reason
    return resp


def _request(
    messages: list[Message] | None = None,
    model: str = _MODEL,
) -> LLMRequest:
    return LLMRequest(
        messages=messages or [Message(role="user", content="ping")],
        model=model,
    )


@pytest.fixture()
def provider(monkeypatch) -> AnthropicProvider:
    monkeypatch.setattr(settings, "anthropic_api_key", _FAKE_KEY)
    return AnthropicProvider()


# ─── name and model support ───────────────────────────────────────────────────

def test_anthropic_provider_name():
    assert AnthropicProvider().name == "anthropic"


def test_anthropic_supports_known_model():
    p = AnthropicProvider()
    assert p.supports_model("claude-3-5-haiku-latest") is True
    assert p.supports_model("claude-3-5-sonnet-latest") is True
    assert p.supports_model("claude-sonnet-4-5") is True
    assert p.supports_model("claude-opus-4-1") is True


def test_anthropic_does_not_support_unknown_model():
    assert AnthropicProvider().supports_model("not-a-real-model-xyz") is False


# ─── config validation ────────────────────────────────────────────────────────

def test_anthropic_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    with pytest.raises(ProviderConfigurationError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider().validate_config(_MODEL)


def test_anthropic_missing_api_key_empty_string_raises(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(ProviderConfigurationError):
        AnthropicProvider().validate_config(_MODEL)


# ─── successful completion ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_success_returns_normalized_response(provider):
    mock_resp = _mock_response(text="Hello world", input_tokens=10, output_tokens=5)

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await provider.complete(_request())

    assert result.provider == "anthropic"
    assert result.model == _MODEL
    assert result.text == "Hello world"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.finish_reason == "end_turn"
    assert isinstance(result, LLMResponse)


# ─── message translation ──────────────────────────────────────────────────────

def test_anthropic_system_messages_become_system_parameter():
    req = _request(messages=[
        Message(role="system", content="You are helpful."),
        Message(role="user", content="ping"),
    ])
    system, messages = _translate_messages(req)

    assert system == "You are helpful."
    assert messages == [{"role": "user", "content": "ping"}]


def test_anthropic_multiple_system_messages_concatenate():
    req = _request(messages=[
        Message(role="system", content="Part one."),
        Message(role="system", content="Part two."),
        Message(role="user", content="go"),
    ])
    system, messages = _translate_messages(req)

    assert system == "Part one.\n\nPart two."
    assert len(messages) == 1


def test_anthropic_assistant_messages_preserved():
    req = _request(messages=[
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
        Message(role="user", content="bye"),
    ])
    system, messages = _translate_messages(req)

    assert system == ""
    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "bye"},
    ]


def test_anthropic_zero_system_message_allowed():
    req = _request(messages=[Message(role="user", content="only user")])
    system, messages = _translate_messages(req)

    assert system == ""
    assert messages == [{"role": "user", "content": "only user"}]


@pytest.mark.asyncio
async def test_anthropic_system_parameter_not_sent_when_empty(provider):
    mock_resp = _mock_response()

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        create_mock = AsyncMock(return_value=mock_resp)
        mock_client.messages.create = create_mock
        mock_cls.return_value = mock_client

        await provider.complete(_request(messages=[Message(role="user", content="hi")]))

    call_kwargs = create_mock.call_args.kwargs
    assert "system" not in call_kwargs


@pytest.mark.asyncio
async def test_anthropic_system_parameter_sent_when_present(provider):
    mock_resp = _mock_response()

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        create_mock = AsyncMock(return_value=mock_resp)
        mock_client.messages.create = create_mock
        mock_cls.return_value = mock_client

        await provider.complete(_request(messages=[
            Message(role="system", content="Be brief."),
            Message(role="user", content="hi"),
        ]))

    call_kwargs = create_mock.call_args.kwargs
    assert call_kwargs["system"] == "Be brief."


# ─── error mapping ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_rate_limit_maps_retryable(provider):
    exc = _sdk_error(anthropic.RateLimitError, "rate limit", 429)

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_anthropic_timeout_maps_retryable(provider):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APITimeoutError(request=req)

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderTimeoutError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_anthropic_auth_maps_non_retryable(provider):
    exc = _sdk_error(anthropic.AuthenticationError, "invalid auth", 401)

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_anthropic_invalid_response_maps_invalid_response(provider):
    exc = _sdk_error(anthropic.BadRequestError, "invalid request body", 400)

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderInvalidResponseError):
            await provider.complete(_request())


# ─── security guards ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_error_does_not_leak_api_key(monkeypatch):
    fake_key = "sk-ant-SECRETKEYABCDEF1234567890xyz12"
    monkeypatch.setattr(settings, "anthropic_api_key", fake_key)
    exc = _sdk_error(
        anthropic.AuthenticationError,
        f"auth failed key={fake_key}",
        401,
    )

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderAuthError) as exc_info:
            await AnthropicProvider().complete(_request())

    assert fake_key not in str(exc_info.value)


@pytest.mark.asyncio
async def test_anthropic_raw_does_not_include_prompt_content(provider):
    sentinel = "SECRET_PROMPT_SENTINEL_DO_NOT_LOG_ANTHROPIC"

    with patch("backend.llm.providers.anthropic.anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_response())
        mock_cls.return_value = mock_client

        result = await provider.complete(
            _request(messages=[Message(role="user", content=sentinel)])
        )

    assert sentinel not in str(result.raw)


# ─── registry ─────────────────────────────────────────────────────────────────

def test_default_registry_includes_anthropic():
    registry = default_registry()
    assert "anthropic" in registry.list()


# ─── role config routing ──────────────────────────────────────────────────────

def test_role_config_can_resolve_anthropic_provider(monkeypatch):
    for attr in (
        "default_llm_provider", "default_llm_model",
        "triage_llm_provider", "triage_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    monkeypatch.setattr(settings, "default_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "default_llm_model", "claude-3-5-haiku-latest")

    config = resolve_role_config(Role.TRIAGE)

    assert config.provider == "anthropic"
    assert config.model == "claude-3-5-haiku-latest"


@pytest.mark.asyncio
async def test_pipeline_role_can_route_to_anthropic_with_mocked_provider(
    monkeypatch, tmp_repo
):
    """get_provider_for_role resolves anthropic when env selects it."""
    from backend.llm import get_provider_for_role

    for attr in (
        "default_llm_provider", "default_llm_model",
        "planner_llm_provider", "planner_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    monkeypatch.setattr(settings, "planner_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "planner_llm_model", "claude-3-5-haiku-latest")

    provider, model = get_provider_for_role(Role.PLANNER)

    assert provider.name == "anthropic"
    assert model == "claude-3-5-haiku-latest"
