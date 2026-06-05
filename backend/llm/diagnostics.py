"""
diagnostics.py
Read-only, per-role LLM provider/model diagnostics (#33E;
docs/design/multi-provider-modes.md).

This module answers "is each role's configured provider/model usable?" using ONLY
the existing resolution + validation paths:

  - resolve_role_config (resolution precedence, unchanged)
  - get_provider_for_role (registry lookup, model-support check, #33D fake guard)
  - provider.validate_config (key/model config check)

It is strictly read-only and side-effect free: it performs NO LLM completion, NO
git, NO route, NO DB, and NO repo work, and it mutates nothing (runs, chunks,
approvals, provenance, PR state, provider config). It never changes routing,
adds fallback, or selects a provider. Messages are sanitized (LLMError already
sanitizes; unexpected errors are passed through sanitize_for_log) so no API key,
secret, or environment dump can leak.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.llm import get_provider_for_role, is_fake_provider_allowed
from backend.llm.errors import (
    LLMError,
    ProviderConfigurationError,
    UnsupportedModelError,
    UnsupportedProviderError,
)
from backend.llm.role_config import Role, resolve_role_config
from backend.llm.sanitize import sanitize_for_log

DiagnosticStatus = Literal["available", "unavailable", "blocked", "unknown"]

_FAKE_PROVIDER_NAME = "fake"

_FAKE_BLOCKED_MESSAGE = (
    "Fake LLM provider is disabled outside tests. Set "
    "PIPEWRIGHT_ALLOW_FAKE_PROVIDER=true only for intentional local testing."
)
_AVAILABLE_MESSAGE = "Provider and model configuration validated."


class RoleDiagnostic(BaseModel):
    """
    Read-only diagnostic for a single role's resolved provider/model.

    Display/observability only — it grants no authority and changes nothing.
    """

    # ``model`` is a plain metadata string; silence pydantic's protected-namespace
    # warning for the literal field name ``model``.
    model_config = ConfigDict(protected_namespaces=())

    role: str
    provider: str
    model: str
    status: DiagnosticStatus
    message: str
    # True when the resolved provider is fake and not explicitly allowed (#33D).
    fake_blocked: bool = False
    # True only when the provider/model/config validation path succeeded.
    validated: bool = False


class LLMDiagnosticsReport(BaseModel):
    """All roles' diagnostics, computed on read. Never persisted."""

    roles: list[RoleDiagnostic]


def diagnose_role(role: Role) -> RoleDiagnostic:
    """
    Diagnose one role using the existing resolution + validation path. Pure and
    read-only; never raises (any error is classified and sanitized).
    """
    try:
        config = resolve_role_config(role)
    except Exception as error:  # resolution itself failed unexpectedly
        return RoleDiagnostic(
            role=role.value,
            provider="",
            model="",
            status="unknown",
            message=sanitize_for_log(str(error)),
        )

    provider_name = (config.provider or "").strip()
    model = config.model
    is_fake = provider_name.lower() == _FAKE_PROVIDER_NAME

    # Fake guard (#33D): a fake provider not explicitly allowed is BLOCKED. Report
    # it without invoking the resolution path (which would raise the same).
    if is_fake and not is_fake_provider_allowed():
        return RoleDiagnostic(
            role=role.value,
            provider=provider_name,
            model=model,
            status="blocked",
            message=_FAKE_BLOCKED_MESSAGE,
            fake_blocked=True,
            validated=False,
        )

    try:
        provider, resolved_model = get_provider_for_role(role)
        validate_config = getattr(provider, "validate_config", None)
        if validate_config:
            validate_config(resolved_model)
        return RoleDiagnostic(
            role=role.value,
            provider=provider_name,
            model=model,
            status="available",
            message=_AVAILABLE_MESSAGE,
            validated=True,
        )
    except ProviderConfigurationError as error:
        # Missing key / provider config problem. (A blocked fake is handled above,
        # so a fake reaching here is allowed; mapping stays defensive.)
        return RoleDiagnostic(
            role=role.value,
            provider=provider_name,
            model=model,
            status="blocked" if is_fake else "unavailable",
            message=error.message,
        )
    except (UnsupportedProviderError, UnsupportedModelError) as error:
        return RoleDiagnostic(
            role=role.value,
            provider=provider_name,
            model=model,
            status="unavailable",
            message=error.message,
        )
    except LLMError as error:
        return RoleDiagnostic(
            role=role.value,
            provider=provider_name,
            model=model,
            status="unknown",
            message=error.message,
        )
    except Exception as error:
        return RoleDiagnostic(
            role=role.value,
            provider=provider_name,
            model=model,
            status="unknown",
            message=sanitize_for_log(str(error)),
        )


def diagnose_all_roles() -> LLMDiagnosticsReport:
    """One diagnostic per role in the Role enum. Read-only; never persisted."""
    return LLMDiagnosticsReport(roles=[diagnose_role(role) for role in Role])
