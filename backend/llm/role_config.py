"""
Role-based LLM provider/model configuration.
"""

from dataclasses import dataclass
from enum import StrEnum
import os

from backend.config.keys import settings


DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_CONTEXT_WINDOW = 8192

MODEL_CONTEXT_WINDOWS = {
    "gemini-2.5-flash-lite": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-1.5-pro": 1_000_000,
    "claude-3-5-haiku-latest": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 128_000,
    "gpt-4.1-mini": 128_000,
    "gpt-5": 128_000,
    "gpt-5-mini": 128_000,
    "o1": 128_000,
    "o3": 128_000,
    "o4-mini": 128_000,
}


class Role(StrEnum):
    TRIAGE = "triage"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    SUMMARY = "summary"
    ARCHITECT = "architect"


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str


def resolve_context_window(model: str | None) -> int:
    model_key = (model or "").strip().lower()
    return MODEL_CONTEXT_WINDOWS.get(model_key, DEFAULT_CONTEXT_WINDOW)


def _env_or_setting(attr_name: str, env_name: str) -> str | None:
    value = os.getenv(env_name)
    if value and value.strip():
        return value.strip()
    value = getattr(settings, attr_name.lower(), None)
    if value:
        return value
    return None


def resolve_role_config(
    role: Role,
    overrides: dict[str, str] | None = None,
) -> RoleConfig:
    role_value = role.value
    overrides = overrides or {}

    provider = overrides.get(f"{role_value}_provider")
    model = overrides.get(f"{role_value}_model")

    if not provider:
        provider = _env_or_setting(
            f"{role_value}_llm_provider",
            f"{role_value.upper()}_LLM_PROVIDER",
        )
    if not model:
        model = _env_or_setting(
            f"{role_value}_llm_model",
            f"{role_value.upper()}_LLM_MODEL",
        )

    if not provider:
        provider = _env_or_setting(
            "default_llm_provider",
            "DEFAULT_LLM_PROVIDER",
        )
    if not model:
        model = _env_or_setting("default_llm_model", "DEFAULT_LLM_MODEL")

    return RoleConfig(
        provider=provider or DEFAULT_PROVIDER,
        model=model or DEFAULT_MODEL,
    )
