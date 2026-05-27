import pytest

from backend.llm.errors import UnsupportedProviderError
from backend.llm.providers.fake import FakeProvider
from backend.llm.registry import ProviderRegistry, default_registry

pytestmark = pytest.mark.unit


def test_registry_register_get_and_list():
    registry = ProviderRegistry()
    provider = FakeProvider()

    registry.register(provider)

    assert registry.get("fake") is provider
    assert registry.list() == ["fake"]


def test_registry_duplicate_registration_fails():
    registry = ProviderRegistry()
    registry.register(FakeProvider())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeProvider())


def test_registry_unknown_provider_raises():
    registry = ProviderRegistry()

    with pytest.raises(UnsupportedProviderError):
        registry.get("missing")


def test_default_registry_registers_all_providers():
    registry = default_registry()

    assert "gemini" in registry.list()
    assert "fake" in registry.list()
    assert "anthropic" in registry.list()
    assert "openai" in registry.list()
