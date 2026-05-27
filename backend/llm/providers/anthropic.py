"""
Anthropic/Claude LLM provider adapter.
"""

from typing import Any

import anthropic

from backend.config.keys import settings
from backend.llm.base import BaseLLMProvider, LLMRequest, LLMResponse
from backend.llm.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderContentFilteredError,
    ProviderExecutionError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    UnsupportedModelError,
)
from backend.llm.sanitize import sanitize_for_log


class AnthropicProvider(BaseLLMProvider):
    _SUPPORTED_MODELS = {
        "claude-3-5-sonnet-latest",
        "claude-3-5-haiku-latest",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-1",
        "claude-opus-4-7",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    }

    @property
    def name(self) -> str:
        return "anthropic"

    def supports_model(self, model: str) -> bool:
        return model in self._SUPPORTED_MODELS

    def validate_config(self, model: str | None = None) -> None:
        if model is not None and not self.supports_model(model):
            raise UnsupportedModelError(
                f"Unsupported Anthropic model: {model}",
                provider=self.name,
                model=model,
                retryable=False,
            )
        api_key = getattr(settings, "anthropic_api_key", None)
        if not api_key:
            raise ProviderConfigurationError(
                "ANTHROPIC_API_KEY is not configured",
                provider=self.name,
                model=model,
                retryable=False,
            )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.validate_config(request.model)
        api_key = settings.anthropic_api_key

        system_prompt, messages = _translate_messages(request)

        try:
            client = anthropic.AsyncAnthropic(
                api_key=api_key,
                timeout=float(request.timeout_seconds),
            )
            kwargs: dict[str, Any] = {
                "model": request.model,
                "max_tokens": request.max_output_tokens,
                "temperature": request.temperature,
                "messages": messages,
            }
            if system_prompt:
                kwargs["system"] = system_prompt

            response = await client.messages.create(**kwargs)
            return _to_llm_response(response, self.name, request.model)

        except (ProviderConfigurationError, UnsupportedModelError):
            raise
        except Exception as error:
            raise _map_anthropic_error(error, self.name, request.model)


def _translate_messages(
    request: LLMRequest,
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content)
        elif message.role in ("user", "assistant"):
            messages.append({"role": message.role, "content": message.content})

    return "\n\n".join(system_parts), messages


def _to_llm_response(response: Any, provider: str, model: str) -> LLMResponse:
    text_parts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text" and hasattr(block, "text")
    ]
    text = "".join(text_parts)

    if not text:
        raise ProviderInvalidResponseError(
            "Anthropic returned an empty or invalid response",
            provider=provider,
            model=model,
            retryable=False,
        )

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage else None
    output_tokens = getattr(usage, "output_tokens", None) if usage else None
    stop_reason = getattr(response, "stop_reason", None)
    finish_reason = str(stop_reason) if stop_reason else None

    return LLMResponse(
        text=text,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        raw={
            "stop_reason": finish_reason,
            "usage_available": usage is not None,
        },
    )


def _map_anthropic_error(error: Exception, provider: str, model: str) -> Exception:
    message = sanitize_for_log(str(error))

    if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ProviderAuthError(
            message, provider=provider, model=model, retryable=False
        )
    if isinstance(error, anthropic.RateLimitError):
        return ProviderRateLimitError(message, provider=provider, model=model)
    if isinstance(error, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return ProviderTimeoutError(message, provider=provider, model=model)
    if isinstance(error, anthropic.BadRequestError):
        lowered = message.lower()
        if any(m in lowered for m in ("safety", "content_policy", "harmful", "blocked")):
            return ProviderContentFilteredError(
                message, provider=provider, model=model, retryable=False
            )
        return ProviderInvalidResponseError(
            message, provider=provider, model=model, retryable=False
        )
    if isinstance(error, anthropic.InternalServerError):
        return ProviderExecutionError(
            message, provider=provider, model=model, retryable=True
        )
    if isinstance(error, anthropic.APIStatusError):
        status_code = getattr(error, "status_code", 0)
        retryable = status_code in (500, 502, 503, 504, 529)
        return ProviderExecutionError(
            message, provider=provider, model=model, retryable=retryable
        )

    return ProviderExecutionError(
        message, provider=provider, model=model, retryable=False
    )
