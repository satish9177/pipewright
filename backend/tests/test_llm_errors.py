import pytest

from backend.llm.errors import (
    LLMError,
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

pytestmark = pytest.mark.unit


def test_llm_error_fields_and_sanitized_string():
    raw_key = "AIzaSyA123456789012345678901234567890"
    error = LLMError(
        f"Provider failed with key {raw_key}",
        provider="gemini",
        model="gemini-2.5-flash-lite",
        retryable=False,
    )

    assert error.provider == "gemini"
    assert error.model == "gemini-2.5-flash-lite"
    assert error.retryable is False
    assert raw_key not in str(error)
    assert "[REDACTED]" in str(error)


def test_retryable_flags_and_rate_limit_retry_after():
    rate_limit = ProviderRateLimitError(
        "429",
        provider="gemini",
        model="gemini-2.5-flash-lite",
        retry_after_seconds=30,
    )
    timeout = ProviderTimeoutError(
        "deadline",
        provider="gemini",
        model="gemini-2.5-flash-lite",
    )

    assert rate_limit.retryable is True
    assert rate_limit.retry_after_seconds == 30
    assert timeout.retryable is True


def test_non_retryable_error_classes():
    kwargs = {
        "provider": "gemini",
        "model": "gemini-2.5-flash-lite",
        "retryable": False,
    }

    assert ProviderAuthError("auth", **kwargs).retryable is False
    assert ProviderContentFilteredError("blocked", **kwargs).retryable is False
    assert ProviderInvalidResponseError("bad", **kwargs).retryable is False
