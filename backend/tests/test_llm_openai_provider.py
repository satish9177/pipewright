"""
test_llm_openai_provider.py
Unit tests for OpenAIProvider. All SDK calls are fully mocked — no live
API calls, no OPENAI_API_KEY required.
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
from backend.llm.providers.openai import OpenAIProvider, _translate_messages
from backend.llm.registry import default_registry
from backend.llm.role_config import Role, resolve_role_config

pytestmark = pytest.mark.unit

# ─── helpers ─────────────────────────────────────────────────────────────────

_FAKE_KEY = "sk-test-fakekey1234567890abcdef"
_MODEL = "gpt-4o-mini"


def _make_httpx_response(status_code: int) -> httpx.Response:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
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
def provider(monkeypatch) -> OpenAIProvider:
    monkeypatch.setattr(settings, "openai_api_key", _FAKE_KEY)
    return OpenAIProvider()


# ─── name and model support ───────────────────────────────────────────────────

def test_openai_provider_name():
    assert OpenAIProvider().name == "openai"


def test_openai_supports_known_model():
    p = OpenAIProvider()
    for model in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-5", "gpt-5-mini"):
        assert p.supports_model(model) is True, f"should support {model}"
    for model in ("o1", "o1-mini", "o3", "o3-mini", "o4-mini"):
        assert p.supports_model(model) is True, f"should support {model}"


def test_openai_does_not_support_invalid_model():
    p = OpenAIProvider()
    assert p.supports_model("") is False
    assert p.supports_model("   ") is False
    assert p.supports_model("fake-model") is False
    assert p.supports_model("claude-3-5-haiku-latest") is False
    assert p.supports_model("gemini-2.5-flash") is False


# ─── config validation ────────────────────────────────────────────────────────

def test_openai_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        OpenAIProvider().validate_config(_MODEL)


def test_openai_missing_api_key_empty_string_raises(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(ProviderConfigurationError):
        OpenAIProvider().validate_config(_MODEL)


# ─── successful completion ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_success_returns_normalized_response(provider):
    mock_resp = _mock_response(text="Hello world", input_tokens=10, output_tokens=5)

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await provider.complete(_request())

    assert result.provider == "openai"
    assert result.model == _MODEL
    assert result.text == "Hello world"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.finish_reason == "stop"
    assert isinstance(result, LLMResponse)


# ─── message translation ──────────────────────────────────────────────────────

def test_openai_system_messages_preserved():
    req = _request(messages=[
        Message(role="system", content="Be helpful."),
        Message(role="user", content="ping"),
    ])
    messages = _translate_messages(req)

    assert messages[0] == {"role": "system", "content": "Be helpful."}
    assert messages[1] == {"role": "user", "content": "ping"}


def test_openai_multiple_system_messages_supported():
    req = _request(messages=[
        Message(role="system", content="Rule one."),
        Message(role="system", content="Rule two."),
        Message(role="user", content="go"),
    ])
    messages = _translate_messages(req)

    assert messages[0] == {"role": "system", "content": "Rule one."}
    assert messages[1] == {"role": "system", "content": "Rule two."}
    assert messages[2] == {"role": "user", "content": "go"}


def test_openai_assistant_messages_preserved():
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


def test_openai_zero_system_message_allowed():
    req = _request(messages=[Message(role="user", content="only user")])
    messages = _translate_messages(req)

    assert messages == [{"role": "user", "content": "only user"}]


# ─── response_format ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_json_object_response_format_passed(provider):
    mock_resp = _mock_response()

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        create_mock = AsyncMock(return_value=mock_resp)
        mock_client.chat.completions.create = create_mock
        mock_cls.return_value = mock_client

        await provider.complete(_request(response_format="json_object"))

    call_kwargs = create_mock.call_args.kwargs
    assert call_kwargs.get("response_format") == {"type": "json_object"}


@pytest.mark.asyncio
async def test_openai_text_response_format_omits_param(provider):
    mock_resp = _mock_response()

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        create_mock = AsyncMock(return_value=mock_resp)
        mock_client.chat.completions.create = create_mock
        mock_cls.return_value = mock_client

        await provider.complete(_request(response_format="text"))

    call_kwargs = create_mock.call_args.kwargs
    assert "response_format" not in call_kwargs


# ─── error mapping ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_rate_limit_maps_retryable(provider):
    exc = _sdk_error(openai_sdk.RateLimitError, "rate limit", 429)

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_openai_timeout_maps_retryable(provider):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    exc = openai_sdk.APITimeoutError(request=req)

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderTimeoutError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_openai_auth_maps_non_retryable(provider):
    exc = _sdk_error(openai_sdk.AuthenticationError, "invalid auth", 401)

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_openai_invalid_response_maps_invalid_response(provider):
    exc = _sdk_error(openai_sdk.BadRequestError, "invalid request body", 400)

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderInvalidResponseError):
            await provider.complete(_request())


@pytest.mark.asyncio
async def test_openai_content_filter_maps_content_filtered(provider):
    exc = openai_sdk.ContentFilterFinishReasonError()

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderContentFilteredError) as exc_info:
            await provider.complete(_request())

    assert exc_info.value.retryable is False


# ─── security guards ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_error_does_not_leak_api_key(monkeypatch):
    fake_key = "sk-test-SECRETKEYABCDEF1234567890abcdef"
    monkeypatch.setattr(settings, "openai_api_key", fake_key)
    exc = _sdk_error(
        openai_sdk.AuthenticationError,
        f"auth failed key={fake_key}",
        401,
    )

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=exc)
        mock_cls.return_value = mock_client

        with pytest.raises(ProviderAuthError) as exc_info:
            await OpenAIProvider().complete(_request())

    assert fake_key not in str(exc_info.value)


@pytest.mark.asyncio
async def test_openai_raw_does_not_include_prompt_content(provider):
    sentinel = "SECRET_PROMPT_SENTINEL_DO_NOT_LOG_OPENAI"

    with patch("backend.llm.providers.openai.openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_response())
        mock_cls.return_value = mock_client

        result = await provider.complete(
            _request(messages=[Message(role="user", content=sentinel)])
        )

    assert sentinel not in str(result.raw)


# ─── registry ─────────────────────────────────────────────────────────────────

def test_default_registry_includes_openai():
    registry = default_registry()
    assert "openai" in registry.list()


# ─── role config routing ──────────────────────────────────────────────────────

def test_role_config_can_resolve_openai_provider(monkeypatch):
    for attr in (
        "default_llm_provider", "default_llm_model",
        "triage_llm_provider", "triage_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    monkeypatch.setattr(settings, "default_llm_provider", "openai")
    monkeypatch.setattr(settings, "default_llm_model", "gpt-4o-mini")

    config = resolve_role_config(Role.TRIAGE)

    assert config.provider == "openai"
    assert config.model == "gpt-4o-mini"


def test_pipeline_role_can_route_to_openai_with_mocked_provider(monkeypatch):
    """get_provider_for_role resolves openai when env selects it."""
    from backend.llm import get_provider_for_role

    for attr in (
        "default_llm_provider", "default_llm_model",
        "coder_llm_provider", "coder_llm_model",
    ):
        monkeypatch.setattr(settings, attr, None)
    monkeypatch.setattr(settings, "coder_llm_provider", "openai")
    monkeypatch.setattr(settings, "coder_llm_model", "gpt-4o-mini")

    provider, model = get_provider_for_role(Role.CODER)

    assert provider.name == "openai"
    assert model == "gpt-4o-mini"
