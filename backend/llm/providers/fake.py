"""
Deterministic fake LLM provider for tests and local scaffolding.
"""

from backend.llm.base import BaseLLMProvider, LLMRequest, LLMResponse


class FakeProvider(BaseLLMProvider):
    _SUPPORTED_MODELS = {"fake-model"}

    @property
    def name(self) -> str:
        return "fake"

    def supports_model(self, model: str) -> bool:
        return model in self._SUPPORTED_MODELS

    async def complete(self, request: LLMRequest) -> LLMResponse:
        text = request.extras.get("response_text", "fake response")
        return LLMResponse(
            text=text,
            provider=self.name,
            model=request.model,
            input_tokens=0,
            output_tokens=len(text.split()),
            finish_reason="stop",
            raw={"fake": True},
        )
