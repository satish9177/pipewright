from types import SimpleNamespace

import pytest

from backend.config.keys import settings
from backend.llm.base import LLMRequest, Message
from backend.llm.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderContentFilteredError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from backend.llm.providers import gemini
from backend.llm.providers.gemini import GeminiProvider

pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(self, text="ok"):
        self.text = text
        self.usage_metadata = SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=3,
        )
        self.candidates = [SimpleNamespace(finish_reason="STOP")]


class CapturingModel:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        CapturingModel.instances.append(self)

    def generate_content(self, contents, request_options=None):
        self.calls.append({
            "contents": contents,
            "request_options": request_options,
        })
        return FakeResponse("normalized")


def _mock_gemini(monkeypatch, model_class=CapturingModel):
    CapturingModel.instances = []
    monkeypatch.setattr(settings, "gemini_api_key", "AIzaSyA123456789012345678901234567890")
    monkeypatch.setattr(gemini.genai, "configure", lambda api_key: None)
    monkeypatch.setattr(
        gemini.genai,
        "GenerationConfig",
        lambda **kwargs: {"generation_config": kwargs},
    )
    monkeypatch.setattr(gemini.genai, "GenerativeModel", model_class)


async def test_gemini_success_returns_normalized_response(monkeypatch):
    _mock_gemini(monkeypatch)
    provider = GeminiProvider()

    response = await provider.complete(
        LLMRequest(
            messages=[Message(role="user", content="hello")],
            model="gemini-2.5-flash-lite",
        )
    )

    assert response.text == "normalized"
    assert response.provider == "gemini"
    assert response.model == "gemini-2.5-flash-lite"
    assert response.input_tokens == 10
    assert response.output_tokens == 3
    assert response.finish_reason == "STOP"


async def test_gemini_translates_system_and_assistant_messages(monkeypatch):
    _mock_gemini(monkeypatch)
    provider = GeminiProvider()

    await provider.complete(
        LLMRequest(
            messages=[
                Message(role="system", content="system one"),
                Message(role="system", content="system two"),
                Message(role="assistant", content="previous"),
                Message(role="user", content="next"),
            ],
            model="gemini-2.5-flash-lite",
            timeout_seconds=12,
        )
    )

    instance = CapturingModel.instances[0]
    assert instance.kwargs["system_instruction"] == "system one\n\nsystem two"
    assert instance.calls[0]["contents"] == [
        {"role": "model", "parts": [{"text": "previous"}]},
        {"role": "user", "parts": [{"text": "next"}]},
    ]
    assert instance.calls[0]["request_options"] == {"timeout": 12}


async def test_gemini_zero_system_message_allowed(monkeypatch):
    _mock_gemini(monkeypatch)
    provider = GeminiProvider()

    await provider.complete(
        LLMRequest(
            messages=[Message(role="user", content="next")],
            model="gemini-2.5-flash-lite",
        )
    )

    assert CapturingModel.instances[0].kwargs["system_instruction"] is None


async def test_gemini_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    provider = GeminiProvider()

    with pytest.raises(ProviderConfigurationError):
        await provider.complete(
            LLMRequest(
                messages=[Message(role="user", content="hello")],
                model="gemini-2.5-flash-lite",
            )
        )


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        ("429 quota exceeded", ProviderRateLimitError),
        ("deadline exceeded", ProviderTimeoutError),
        ("permission denied for API key AIzaSyA123456789012345678901234567890", ProviderAuthError),
        ("response blocked by safety filter", ProviderContentFilteredError),
    ],
)
async def test_gemini_error_mapping_and_sanitization(
    monkeypatch,
    message,
    error_type,
):
    class ErrorModel(CapturingModel):
        def generate_content(self, contents, request_options=None):
            raise RuntimeError(message)

    _mock_gemini(monkeypatch, ErrorModel)
    provider = GeminiProvider()

    with pytest.raises(error_type) as exc_info:
        await provider.complete(
            LLMRequest(
                messages=[Message(role="user", content="hello")],
                model="gemini-2.5-flash-lite",
            )
        )

    assert "AIzaSyA" not in str(exc_info.value)


async def test_gemini_invalid_response_maps_to_invalid_response(monkeypatch):
    class InvalidModel(CapturingModel):
        def generate_content(self, contents, request_options=None):
            return SimpleNamespace(text=None, usage_metadata=None, candidates=[])

    _mock_gemini(monkeypatch, InvalidModel)
    provider = GeminiProvider()

    with pytest.raises(ProviderInvalidResponseError):
        await provider.complete(
            LLMRequest(
                messages=[Message(role="user", content="hello")],
                model="gemini-2.5-flash-lite",
            )
        )
