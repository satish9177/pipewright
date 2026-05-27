import pytest

from backend.llm.base import LLMRequest, Message
from backend.llm.providers.fake import FakeProvider

pytestmark = pytest.mark.unit


async def test_fake_provider_default_response():
    provider = FakeProvider()
    response = await provider.complete(
        LLMRequest(
            messages=[Message(role="user", content="hello")],
            model="fake-model",
        )
    )

    assert response.text == "fake response"
    assert response.provider == "fake"
    assert response.model == "fake-model"


async def test_fake_provider_response_text_override():
    provider = FakeProvider()
    response = await provider.complete(
        LLMRequest(
            messages=[Message(role="user", content="hello")],
            model="fake-model",
            extras={"response_text": "custom"},
        )
    )

    assert response.text == "custom"


def test_fake_provider_supports_model():
    provider = FakeProvider()

    assert provider.supports_model("fake-model") is True
    assert provider.supports_model("other") is False
