"""
test_llm_deepseek_provider.py
Unit tests for DeepSeekProvider. All SDK calls are fully mocked — no live
API calls, no DEEPSEEK_API_KEY required.
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import openai as openai_sdk

from backend.config.keys import settings
from backend.llm.base import LLMRequest, LLMResponse, Message
from backend.llm.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderContentFilteredError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from backend.llm.providers.deepseek import DeepSeekProvider, _translate_messages
from backend.llm.registry import default_registry
from backend.llm.role_config import Role, resolve_role_config

pytestmark = pytest.mark.unit

# ─── helpers ─────────────────────────────────────────────────────────────────

_FAKE_KEY = "sk-test-deepseekfakekey1234567890abcdef"
_MODEL = "deepseek-chat"


def _make_httpx_response(status_code: int) -> httpx.Response:
    req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    return httpx.Response(status_code, request=req, text="{}")


def _sdk_error(cls, message: str, status_code: int) -> Exception:
    return cls(message, response=_make_httpx_response(status_code), body={})


def _mock_response(
    text: str = "ok",
    input_tokens: int = 10,
    output_tokens: int = 5,
    finish_reason: str = "stop",
) -> MagicMock:
    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=input_tokens, completion_tokens=output_tokens)
    return resp


def _request(
    messages: list[Message] | None = None,
    model: str = _MODEL,
    response_format: str = "text",
) -> LLMRequest:
    return LLMRequest(
        messages=messages or [Message(role="user", content="ping")],
        model=model,
        response_format=response_format,
    )


@pytest.fixture()
def provider(monkeypatch) -> DeepSeekProvider:
    monkeypatch.setattr(settings, "deepseek_api_key", _FAKE_KEY)
    return DeepSeekProvider()


# ─── name and model support ───────────────────────────────────────────────────

def test_deepseek_provider_name():
    assert DeepSeekProvider().name == "deepseek"


def test_deepseek_supports_known_models():
    p = DeepSeekProvider()
    for model in (
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-coder",
    ):
        assert p.supports_model(model) is True, f"should support {model}"


def test_deepseek_rejects_empty_or_invalid_model():
    p = DeepSeekProvider()
    assert p.supports_model("") is False
    assert p.supports_model("   ") is False
    assert p.supports_model("gpt-4o") is False
    assert p.supports_model("claude-3-5-haiku-latest") is False
    assert p.supports_model("gemini-2.5-flash") is False
    assert p.supports_model("fake-model") is False


# ─── config validation ────────────────────────────────────────────────────────

def test_deepseek_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    with pytest.raises(ProviderConfigurationError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider().validate_config(_MODEL)


def test_deepseek_missing_api_key_empty_string_raises(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    with pytest.raises(ProviderConfigurationError):
        DeepSeekProvider().validate_config(_MODEL)


# ─── client construction ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_uses_openai_client_with_deepseek_base_url(provider):
    mock_resp = _mock_response()

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        await provider.complete(_request())

    call_kwargs = mock_cls.call_args.kwargs
    assert "api.deepseek.com" in call_kwargs.get("base_url", "")


# ─── successful completion ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_success_returns_normalized_response(provider):
    mock_resp = _mock_response(text="Hello world", input_tokens=10, output_tokens=5)

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await provider.complete(_request())

    assert result.provider == "deepseek"
    assert result.model == _MODEL
    assert result.text == "Hello world"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.finish_reason == "stop"
    assert isinstance(result, LLMResponse)


# ─── message translation ──────────────────────────────────────────────────────

def test_deepseek_system_messages_preserved_or_concatenated():
    req = _request(messages=[
        Message(role="system", content="Be helpful."),
        Message(role="user", content="ping"),
    ])
    messages = _translate_messages(req)

    assert messages[0] == {"role": "system", "content": "Be helpful."}
    assert messages[1] == {"role": "user", "content": "ping"}


def test_deepseek_assistant_messages_preserved():
    req = _request(messages=[
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi"),
        Message(role="user", content="bye"),
    ])
    messages = _translate_messages(req)

    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "bye"},
    ]


def test_deepseek_zero_system_message_allowed():
    req = _request(messages=[Message(role="user", content="only user")])
    messages = _translate_messages(req)

    assert messages == [{"role": "user", "content": "only user"}]


# ─── response_format ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_json_object_response_format_best_effort(provider):
    mock_resp = _mock_response()

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        create_mock = AsyncMock(return_value=mock_resp)
        mock_client.chat.completions.create = create_mock
        mock_cls.return_value = mock_client

        await provider.complete(_request(response_format="json_object"))

    call_kwargs = create_mock.call_args.kwargs
    assert call_kwargs.get("response_format") == {"type": "json_object"}


# ─── error mapping ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_rate_limit_maps_retryable(provider):
    exc = _sdk_error(openai_sdk.RateLimitError, "rate limit", 429)

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_deepseek_timeout_maps_retryable(provider):
    req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    exc = openai_sdk.APITimeoutError(request=req)

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderTimeoutError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_deepseek_auth_maps_non_retryable(provider):
    exc = _sdk_error(openai_sdk.AuthenticationError, "invalid auth", 401)

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_deepseek_invalid_response_maps_invalid_response(provider):
    exc = _sdk_error(openai_sdk.BadRequestError, "invalid request body", 400)

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderInvalidResponseError):
            await provider.complete(_request())


# ─── security guards ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_error_does_not_leak_api_key(monkeypatch):
    fake_key = "sk-test-SECRETKEYABCDEF1234567890abcdef"
    monkeypatch.setattr(settings, "deepseek_api_key", fake_key)
    exc = _sdk_error(
        openai_sdk.AuthenticationError,
        f"auth failed key={fake_key}",
        401,
    )

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderAuthError) as exc_info:
            await DeepSeekProvider().complete(_request())

    assert fake_key not in str(exc_info.value)


@pytest.mark.asyncio
async def test_deepseek_raw_does_not_include_prompt_content(provider):
    sentinel = "SECRET_PROMPT_SENTINEL_DO_NOT_LOG_DEEPSEEK"

    with patch("backend.llm.providers.deepseek.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_response())
        mock_cls.return_value = mock_client

        result = await provider.complete(
            _request(messages=[Message(role="user", content=sentinel)])
        )

    assert sentinel not in str(result.raw)


# ─── registry ─────────────────────────────────────────────────────────────────

def test_default_registry_includes_deepseek():
    registry = default_registry()
    assert "deepseek" in registry.list()


# ─── role config routing ──────────────────────────────────────────────────────

def test_role_config_can_resolve_deepseek_provider(monkeypatch, clear_llm_env):
    for attr in (
        "default_llm_provider", "default_llm_model",
        "triage_llm_provider", "triage_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    monkeypatch.setattr(settings, "default_llm_provider", "deepseek")
    monkeypatch.setattr(settings, "default_llm_model", "deepseek-v4-flash")

    config = resolve_role_config(Role.TRIAGE)

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-flash"


def test_pipeline_role_can_route_to_deepseek_with_mocked_provider(monkeypatch, clear_llm_env):
    """get_provider_for_role resolves deepseek when env selects it."""
    from backend.llm import get_provider_for_role

    for attr in (
        "default_llm_provider", "default_llm_model",
        "planner_llm_provider", "planner_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    monkeypatch.setattr(settings, "planner_llm_provider", "deepseek")
    monkeypatch.setattr(settings, "planner_llm_model", "deepseek-v4-flash")

    provider, model = get_provider_for_role(Role.PLANNER)

    assert provider.name == "deepseek"
    assert model == "deepseek-v4-flash"
