"""
Public LLM provider abstraction API.
"""

import logging
import os
from collections.abc import Mapping

from backend.llm.base import BaseLLMProvider, LLMRequest, LLMResponse, Message
from backend.llm.errors import ProviderConfigurationError, UnsupportedModelError
from backend.llm.registry import default_registry
from backend.llm.role_config import Role, RoleConfig, resolve_role_config

logger = logging.getLogger(__name__)

# FakeProvider stays registered (tests rely on it), but it must never be reachable
# for a real run. It is allowed only inside the test runner or when explicitly
# opted in. Truthy values for the opt-in flag (case-insensitive).
_FAKE_PROVIDER_NAME = "fake"
_FAKE_ALLOW_TRUTHY = frozenset({"true", "1", "yes", "on"})


def is_fake_provider_allowed(env: Mapping[str, str] | None = None) -> bool:
    """
    Whether the ``fake`` LLM provider may be selected for runtime resolution (#33D).

    Allowed only when running under pytest (``PYTEST_CURRENT_TEST`` is present) or
    when ``PIPEWRIGHT_ALLOW_FAKE_PROVIDER`` is an explicit truthy value. Pure and
    testable: reads the supplied ``env`` (defaults to ``os.environ``) and nothing
    else. Does not affect any other provider.
    """
    env = os.environ if env is None else env
    if env.get("PYTEST_CURRENT_TEST"):
        return True
    flag = (env.get("PIPEWRIGHT_ALLOW_FAKE_PROVIDER") or "").strip().lower()
    return flag in _FAKE_ALLOW_TRUTHY


def get_provider_for_role(
    role: Role,
    overrides: dict[str, str] | None = None,
) -> tuple[BaseLLMProvider, str]:
    config = resolve_role_config(role, overrides)
    # Fail closed: refuse the fake provider for real runs before any completion can
    # run. This is the single resolution chokepoint (complete_for_role and
    # validate_all_roles both route through here); direct registry injection in
    # tests bypasses it by design. Resolution precedence is unchanged — this only
    # rejects an already-resolved 'fake'.
    if (config.provider or "").strip().lower() == _FAKE_PROVIDER_NAME and \
            not is_fake_provider_allowed():
        raise ProviderConfigurationError(
            "Fake LLM provider is disabled outside tests. Set "
            "PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true only for intentional local "
            "testing.",
            provider=_FAKE_PROVIDER_NAME,
            model=config.model,
            retryable=False,
        )
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
    "is_fake_provider_allowed",
    "log_token_usage",
    "resolve_role_config",
    "validate_all_roles",
]
