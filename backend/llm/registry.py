"""
Provider registry for LLM provider implementations.
"""

from backend.llm.base import BaseLLMProvider
from backend.llm.errors import UnsupportedProviderError


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseLLMProvider] = {}

    def register(self, provider: BaseLLMProvider) -> None:
        name = provider.name
        if name in self._providers:
            raise ValueError(f"Provider already registered: {name}")
        self._providers[name] = provider

    def get(self, name: str) -> BaseLLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise UnsupportedProviderError(
                f"Unsupported LLM provider: {name}",
                provider=name,
                model=None,
                retryable=False,
            )

    def list(self) -> list[str]:
        return sorted(self._providers)


def default_registry() -> ProviderRegistry:
    from backend.llm.providers.anthropic import AnthropicProvider
    from backend.llm.providers.fake import FakeProvider
    from backend.llm.providers.gemini import GeminiProvider

    registry = ProviderRegistry()
    registry.register(GeminiProvider())
    registry.register(AnthropicProvider())
    registry.register(FakeProvider())
    return registry
