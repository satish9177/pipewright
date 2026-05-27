"""
Role-based LLM provider/model configuration.
"""

from dataclasses import dataclass
from enum import StrEnum
import os

from backend.config.keys import settings


DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash-lite"


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


def _setting_or_env(attr_name: str, env_name: str) -> str | None:
    value = getattr(settings, attr_name.lower(), None)
    if value:
        return value
    value = os.getenv(env_name)
    if value and value.strip():
        return value.strip()
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
        provider = _setting_or_env(
            f"{role_value}_llm_provider",
            f"{role_value.upper()}_LLM_PROVIDER",
        )
    if not model:
        model = _setting_or_env(
            f"{role_value}_llm_model",
            f"{role_value.upper()}_LLM_MODEL",
        )

    if not provider:
        provider = _setting_or_env(
            "default_llm_provider",
            "DEFAULT_LLM_PROVIDER",
        )
    if not model:
        model = _setting_or_env("default_llm_model", "DEFAULT_LLM_MODEL")

    return RoleConfig(
        provider=provider or DEFAULT_PROVIDER,
        model=model or DEFAULT_MODEL,
    )
