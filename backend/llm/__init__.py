"""
Public LLM provider abstraction API.
"""

import logging

from backend.llm.base import BaseLLMProvider, LLMRequest, LLMResponse, Message
from backend.llm.errors import UnsupportedModelError
from backend.llm.registry import default_registry
from backend.llm.role_config import Role, RoleConfig, resolve_role_config

logger = logging.getLogger(__name__)


def get_provider_for_role(
    role: Role,
    overrides: dict[str, str] | None = None,
) -> tuple[BaseLLMProvider, str]:
    config = resolve_role_config(role, overrides)
    provider = default_registry().get(config.provider)
    if not provider.supports_model(config.model):
        raise UnsupportedModelError(
            f"Provider {config.provider} does not support model {config.model}",
            provider=config.provider,
            model=config.model,
            retryable=False,
        )
    return provider, config.model


async def complete_for_role(
    role: Role,
    request: LLMRequest,
    overrides: dict[str, str] | None = None,
) -> LLMResponse:
    provider, model = get_provider_for_role(role, overrides)
    effective_request = request.model_copy(update={"model": model})
    return await provider.complete(effective_request)


def log_token_usage(
    response: LLMResponse,
    *,
    run_id: str | None = None,
    role: Role,
) -> None:
    input_tokens = (
        response.input_tokens if response.input_tokens is not None else "unavailable"
    )
    output_tokens = (
        response.output_tokens if response.output_tokens is not None else "unavailable"
    )
    logger.info(
        "[LLM] role=%s provider=%s model=%s input_tokens=%s output_tokens=%s "
        "finish_reason=%s run_id=%s",
        role.value,
        response.provider,
        response.model,
        input_tokens,
        output_tokens,
        response.finish_reason or "unknown",
        run_id or "none",
    )


def validate_all_roles(overrides: dict[str, str] | None = None) -> None:
    for role in Role:
        provider, model = get_provider_for_role(role, overrides)
        validate_config = getattr(provider, "validate_config", None)
        if validate_config:
            validate_config(model)


__all__ = [
    "BaseLLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "Role",
    "RoleConfig",
    "complete_for_role",
    "get_provider_for_role",
    "log_token_usage",
    "resolve_role_config",
    "validate_all_roles",
]
