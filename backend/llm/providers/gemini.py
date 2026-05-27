"""
Gemini LLM provider adapter.
"""

import asyncio
from typing import Any

import google.generativeai as genai

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


class GeminiProvider(BaseLLMProvider):
    _SUPPORTED_MODELS = {
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    }

    @property
    def name(self) -> str:
        return "gemini"

    def supports_model(self, model: str) -> bool:
        return model in self._SUPPORTED_MODELS

    def validate_config(self, model: str | None = None) -> None:
        if model is not None and not self.supports_model(model):
            raise UnsupportedModelError(
                f"Unsupported Gemini model: {model}",
                provider=self.name,
                model=model,
                retryable=False,
            )
        if not settings.gemini_api_key:
            raise ProviderConfigurationError(
                "GEMINI_API_KEY is not configured",
                provider=self.name,
                model=model,
                retryable=False,
            )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.validate_config(request.model)
        api_key = settings.gemini_api_key

        system_instruction, contents = _translate_messages(request)

        try:
            genai.configure(api_key=api_key)
            generation_config = genai.GenerationConfig(
                temperature=request.temperature,
                max_output_tokens=request.max_output_tokens,
            )
            model = genai.GenerativeModel(
                model_name=request.model,
                generation_config=generation_config,
                system_instruction=system_instruction or None,
            )
            response = await asyncio.to_thread(
                model.generate_content,
                contents,
                request_options={"timeout": request.timeout_seconds},
            )
            return _to_llm_response(response, self.name, request.model)
        except (
            ProviderConfigurationError,
            ProviderInvalidResponseError,
            UnsupportedModelError,
        ):
            raise
        except Exception as error:
            raise _map_gemini_error(error, self.name, request.model)


def _translate_messages(request: LLMRequest) -> tuple[str, list[dict[str, Any]]]:
    system_messages: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in request.messages:
        if message.role == "system":
            system_messages.append(message.content)
        elif message.role == "assistant":
            contents.append({
                "role": "model",
                "parts": [{"text": message.content}],
            })
        else:
            contents.append({
                "role": "user",
                "parts": [{"text": message.content}],
            })

    return "\n\n".join(system_messages), contents


def _to_llm_response(response: Any, provider: str, model: str) -> LLMResponse:
    text = getattr(response, "text", None)
    if not isinstance(text, str) or text == "":
        raise ProviderInvalidResponseError(
            "Gemini returned an empty or invalid response",
            provider=provider,
            model=model,
            retryable=False,
        )

    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    output_tokens = (
        getattr(usage, "candidates_token_count", None) if usage else None
    )
    finish_reason = _finish_reason(response)

    return LLMResponse(
        text=text,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        raw={
            "finish_reason": finish_reason,
            "usage_available": usage is not None,
        },
    )


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    return str(getattr(candidates[0], "finish_reason", None) or "") or None


def _map_gemini_error(error: Exception, provider: str, model: str) -> Exception:
    message = sanitize_for_log(str(error))
    lowered = message.lower()

    if any(marker in lowered for marker in ("429", "rate limit", "quota")):
        return ProviderRateLimitError(
            message,
            provider=provider,
            model=model,
            retry_after_seconds=None,
        )
    if any(marker in lowered for marker in ("timeout", "deadline")):
        return ProviderTimeoutError(message, provider=provider, model=model)
    if any(
        marker in lowered
        for marker in ("api key", "permission", "unauthorized", "forbidden")
    ):
        return ProviderAuthError(
            message,
            provider=provider,
            model=model,
            retryable=False,
        )
    if any(marker in lowered for marker in ("safety", "blocked", "filter")):
        return ProviderContentFilteredError(
            message,
            provider=provider,
            model=model,
            retryable=False,
        )
    if any(
        marker in lowered
        for marker in ("invalid response", "malformed", "empty response")
    ):
        return ProviderInvalidResponseError(
            message,
            provider=provider,
            model=model,
            retryable=False,
        )
    if any(
        marker in lowered
        for marker in ("500", "502", "503", "504", "unavailable", "internal")
    ):
        return ProviderExecutionError(
            message,
            provider=provider,
            model=model,
            retryable=True,
        )
    return ProviderExecutionError(
        message,
        provider=provider,
        model=model,
        retryable=False,
    )
