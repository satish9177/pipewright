"""
DeepSeek LLM provider adapter.
Uses the OpenAI-compatible API at https://api.deepseek.com.
"""

from typing import Any

import openai

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

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Accept any model starting with "deepseek-"; also covers common aliases.
_KNOWN_MODELS = {
    "deepseek-chat",
    "deepseek-reasoner",
}


class DeepSeekProvider(BaseLLMProvider):

    @property
    def name(self) -> str:
        return "deepseek"

    def supports_model(self, model: str) -> bool:
        if not model or not model.strip():
            return False
        return model.startswith("deepseek-") or model in _KNOWN_MODELS

    def validate_config(self, model: str | None = None) -> None:
        if model is not None and not self.supports_model(model):
            raise UnsupportedModelError(
                f"Unsupported DeepSeek model: {model}",
                provider=self.name,
                model=model,
                retryable=False,
            )
        api_key = getattr(settings, "deepseek_api_key", None)
        if not api_key:
            raise ProviderConfigurationError(
                "DEEPSEEK_API_KEY is not configured",
                provider=self.name,
                model=model,
                retryable=False,
            )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.validate_config(request.model)
        api_key = settings.deepseek_api_key
        base_url = getattr(settings, "deepseek_base_url", None) or _DEEPSEEK_BASE_URL

        messages = _translate_messages(request)

        try:
            client = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=float(request.timeout_seconds),
                max_retries=0,
            )
            kwargs: dict[str, Any] = {
                "model": request.model,
                "messages": messages,
                "max_tokens": request.max_output_tokens,
                "temperature": request.temperature,
            }
            if request.response_format == "json_object":
                kwargs["response_format"] = {"type": "json_object"}

            response = await client.chat.completions.create(**kwargs)
            return _to_llm_response(response, self.name, request.model)

        except (ProviderConfigurationError, UnsupportedModelError):
            raise
        except Exception as error:
            raise _map_deepseek_error(error, self.name, request.model)


def _translate_messages(request: LLMRequest) -> list[dict[str, Any]]:
    # DeepSeek OpenAI-compatible API supports system/user/assistant roles natively.
    return [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]


def _to_llm_response(response: Any, provider: str, model: str) -> LLMResponse:
    choices = getattr(response, "choices", None)
    if not choices:
        raise ProviderInvalidResponseError(
            "DeepSeek returned an empty choices list",
            provider=provider,
            model=model,
            retryable=False,
        )

    text = getattr(choices[0].message, "content", None) or ""
    if not text:
        raise ProviderInvalidResponseError(
            "DeepSeek returned an empty or invalid response",
            provider=provider,
            model=model,
            retryable=False,
        )

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    finish_reason = getattr(choices[0], "finish_reason", None)

    return LLMResponse(
        text=text,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=str(finish_reason) if finish_reason else None,
        raw={
            "finish_reason": str(finish_reason) if finish_reason else None,
            "usage_available": usage is not None,
        },
    )


def _map_deepseek_error(error: Exception, provider: str, model: str) -> Exception:
    message = sanitize_for_log(str(error))

    if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderAuthError(
            message, provider=provider, model=model, retryable=False
        )
    if isinstance(error, openai.RateLimitError):
        return ProviderRateLimitError(message, provider=provider, model=model)
    if isinstance(error, (openai.APITimeoutError, openai.APIConnectionError)):
        return ProviderTimeoutError(message, provider=provider, model=model)
    if isinstance(error, openai.ContentFilterFinishReasonError):
        return ProviderContentFilteredError(
            message, provider=provider, model=model, retryable=False
        )
    if isinstance(error, openai.BadRequestError):
        lowered = message.lower()
        if any(
            m in lowered
            for m in ("safety", "content_policy", "harmful", "blocked", "content_filter")
        ):
            return ProviderContentFilteredError(
                message, provider=provider, model=model, retryable=False
            )
        return ProviderInvalidResponseError(
            message, provider=provider, model=model, retryable=False
        )
    if isinstance(error, openai.InternalServerError):
        return ProviderExecutionError(
            message, provider=provider, model=model, retryable=True
        )
    if isinstance(error, openai.APIStatusError):
        status_code = getattr(error, "status_code", 0)
        retryable = status_code in (500, 502, 503, 504, 529)
        return ProviderExecutionError(
            message, provider=provider, model=model, retryable=retryable
        )

    return ProviderExecutionError(
        message, provider=provider, model=model, retryable=False
    )
