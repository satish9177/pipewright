"""
Structured LLM provider errors.
"""

from backend.llm.sanitize import sanitize_for_log


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.retryable = retryable
        self.message = sanitize_for_log(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        model_part = f" model={self.model}" if self.model else ""
        return (
            f"{self.__class__.__name__}"
            f"(provider={self.provider}{model_part}, "
            f"retryable={self.retryable}): {self.message}"
        )


class ProviderConfigurationError(LLMError):
    pass


class UnsupportedProviderError(LLMError):
    pass


class UnsupportedModelError(LLMError):
    pass


class ProviderExecutionError(LLMError):
    pass


class ProviderRateLimitError(LLMError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=True,
        )


class ProviderTimeoutError(LLMError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=True,
        )


class ProviderAuthError(LLMError):
    pass


class ProviderContentFilteredError(LLMError):
    pass


class ProviderInvalidResponseError(LLMError):
    pass
