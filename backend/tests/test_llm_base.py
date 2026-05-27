import inspect

import pytest

from backend.llm.base import BaseLLMProvider, LLMRequest, LLMResponse, Message

pytestmark = pytest.mark.unit


def test_llm_base_models_construct_with_defaults():
    request = LLMRequest(
        messages=[Message(role="user", content="Hello")],
        model="gemini-2.5-flash-lite",
    )

    assert request.temperature == 0.2
    assert request.max_output_tokens == 2000
    assert request.timeout_seconds == 60
    assert request.response_format == "text"
    assert request.extras == {}

    response = LLMResponse(
        text="ok",
        provider="fake",
        model="fake-model",
    )
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.finish_reason is None
    assert response.raw == {}


def test_base_provider_complete_is_async_contract():
    assert inspect.iscoroutinefunction(BaseLLMProvider.complete)
