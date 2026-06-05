"""
startup_diagnostics.py
Log-only local-first setup diagnostics (#32B).

Runs once at startup to surface common local setup problems so a first-time
user gets a clear warning instead of a late, cryptic failure deep in a run.

Hard rules:
  * Log-only. This module NEVER raises out of run_startup_diagnostics() and
    NEVER hard-fails startup. Optional gaps warn; they do not block.
  * It NEVER logs secret values (encryption keys, API keys, tokens). It reports
    only presence/validity, never the value itself.
  * It is cheap: no network calls. The only subprocess is the existing,
    short-timeout `gh auth status` check, and only when a project actually uses
    github_cli mode.

It also emits the local-only exposure warning when the backend host is
configured (via PIPEWRIGHT_BACKEND_HOST) to a non-loopback address, because
Pipewright has no hosted authentication.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Sentinel so callers/tests can pass an explicit value (including None/"") and
# still be distinguished from "not provided, read from env/config".
_UNSET = object()

BACKEND_HOST_ENV = "PIPEWRIGHT_BACKEND_HOST"
ENCRYPTION_KEY_ENV = "PIPEWRIGHT_ENCRYPTION_KEY"

# Hosts that keep the backend on the local machine only. Anything else exposes
# the unauthenticated API to other hosts on the network.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_BACKEND_HOST = "127.0.0.1"

# Providers whose key presence we can check. Custom/unknown providers are
# skipped silently (we cannot know their key requirement here).
_PROVIDER_KEY_ATTR = {
    "gemini": "gemini_api_key",
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "deepseek": "deepseek_api_key",
}
_PROVIDER_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


@dataclass(frozen=True)
class Diagnostic:
    """A single log-only finding. `message` must never contain a secret value."""
    level: str  # "warning" | "info"
    code: str
    message: str


def check_backend_host(host=_UNSET) -> list[Diagnostic]:
    """
    Warn (log-only) when the configured backend host is non-loopback.

    The app does not know uvicorn's actual bind host, so this reads the explicit
    PIPEWRIGHT_BACKEND_HOST env var. If unset, we assume the safe local default
    and emit nothing.
    """
    if host is _UNSET:
        host = os.getenv(BACKEND_HOST_ENV) or DEFAULT_BACKEND_HOST
    normalized = (host or "").strip().lower()
    if not normalized or normalized in LOOPBACK_HOSTS:
        return []
    return [Diagnostic(
        level="warning",
        code="non_loopback_host",
        message=(
            "Pipewright is running in local-trusted mode and has no hosted "
            "authentication. Do not expose the backend beyond localhost. "
            f"Current configured host: {normalized}"
        ),
    )]


def check_encryption_key(key=_UNSET) -> list[Diagnostic]:
    """
    Diagnose PIPEWRIGHT_ENCRYPTION_KEY. Never logs the key value.

    Missing -> warn that encrypted GitHub token storage will not work.
    Present but not a valid Fernet key -> warn (no value shown).
    Valid -> no diagnostic.
    """
    if key is _UNSET:
        try:
            from backend.config.keys import settings
            key = settings.pipewright_encryption_key or os.getenv(ENCRYPTION_KEY_ENV)
        except Exception:
            key = os.getenv(ENCRYPTION_KEY_ENV)

    if not key:
        return [Diagnostic(
            level="warning",
            code="encryption_key_missing",
            message=(
                f"{ENCRYPTION_KEY_ENV} is not set. Encrypted GitHub token "
                "storage will not work until it is set. Local-only runs that "
                "do not store a GitHub token are unaffected."
            ),
        )]

    try:
        from cryptography.fernet import Fernet
        Fernet(key.encode("utf-8"))
    except Exception:
        # Deliberately do not include the exception or the key in the message.
        return [Diagnostic(
            level="warning",
            code="encryption_key_invalid",
            message=(
                f"{ENCRYPTION_KEY_ENV} is set but is not a valid Fernet key. "
                "Generate one with Fernet.generate_key(). Token decryption will "
                "fail until this is corrected."
            ),
        )]
    return []


def _selected_providers() -> set[str]:
    """Distinct lowercased providers resolved across all roles (best-effort)."""
    try:
        from backend.llm.role_config import Role, resolve_role_config
    except Exception:
        return set()
    providers: set[str] = set()
    for role in Role:
        try:
            providers.add(resolve_role_config(role).provider.strip().lower())
        except Exception:
            continue
    return providers


def _provider_key_present(provider: str) -> bool:
    attr = _PROVIDER_KEY_ATTR.get(provider)
    env_name = _PROVIDER_KEY_ENV.get(provider)
    if attr:
        try:
            from backend.config.keys import settings
            value = getattr(settings, attr, None)
            if value and str(value).strip():
                return True
        except Exception:
            pass
    if env_name:
        value = os.getenv(env_name)
        if value and value.strip():
            return True
    return False


def check_provider_keys(selected_providers=_UNSET) -> list[Diagnostic]:
    """
    Warn only for providers actually selected by role config that lack a key.

    Never logs key values. Unknown/custom providers are skipped (we cannot know
    their key requirement).
    """
    if selected_providers is _UNSET:
        selected_providers = _selected_providers()

    diagnostics: list[Diagnostic] = []
    for provider in sorted(selected_providers):
        provider = (provider or "").strip().lower()
        if provider not in _PROVIDER_KEY_ATTR:
            continue
        if not _provider_key_present(provider):
            env_name = _PROVIDER_KEY_ENV[provider]
            diagnostics.append(Diagnostic(
                level="warning",
                code="provider_key_missing",
                message=(
                    f"Provider '{provider}' is selected by LLM role config but "
                    f"{env_name} is not set. Runs that use this provider will "
                    "fail until the key is configured."
                ),
            ))
    return diagnostics


def check_github_cli(projects) -> list[Diagnostic]:
    """
    If any project uses github_cli mode, warn when gh is unavailable or not
    authenticated. The gh check output is never logged (handled in gh_cli).
    """
    uses_github_cli = any(
        (p.get("pr_mode") or "").strip().lower() == "github_cli"
        for p in projects
    )
    if not uses_github_cli:
        return []

    try:
        from backend.git.gh_cli import is_gh_authenticated, is_gh_installed
    except Exception:
        return []

    if not is_gh_installed():
        return [Diagnostic(
            level="warning",
            code="gh_not_installed",
            message=(
                "A project uses pr_mode=github_cli but the GitHub CLI (gh) is "
                "not installed or not on PATH. PR creation in that mode will "
                "fail until gh is installed and authenticated."
            ),
        )]
    if not is_gh_authenticated():
        return [Diagnostic(
            level="warning",
            code="gh_not_authenticated",
            message=(
                "A project uses pr_mode=github_cli but the GitHub CLI (gh) is "
                "not authenticated. Run `gh auth login`. PR creation in that "
                "mode will fail until gh is authenticated."
            ),
        )]
    return []


def check_projects(projects) -> list[Diagnostic]:
    """
    Best-effort, cheap, filesystem-only project checks. No git subprocess.

    Warns when a project's repo_path is missing/non-existent or has no .git, or
    when no test command is configured. Never hard-fails.
    """
    from pathlib import Path

    diagnostics: list[Diagnostic] = []
    for project in projects:
        name = project.get("name") or project.get("id") or "<unknown>"
        repo_path = (project.get("repo_path") or "").strip()
        if not repo_path:
            diagnostics.append(Diagnostic(
                level="warning",
                code="repo_path_missing",
                message=f"Project '{name}' has no repo_path configured.",
            ))
        else:
            path = Path(repo_path)
            if not path.exists():
                diagnostics.append(Diagnostic(
                    level="warning",
                    code="repo_path_not_found",
                    message=(
                        f"Project '{name}' repo_path does not exist: {repo_path}"
                    ),
                ))
            elif not (path / ".git").exists():
                # .git may be a dir (normal repo) or a file (worktree); both
                # exist(). Absence means this is not a git repo root.
                diagnostics.append(Diagnostic(
                    level="warning",
                    code="repo_path_not_git",
                    message=(
                        f"Project '{name}' repo_path is not a git repository "
                        f"(no .git found): {repo_path}"
                    ),
                ))

        if not (project.get("test_command") or "").strip():
            diagnostics.append(Diagnostic(
                level="warning",
                code="test_command_missing",
                message=(
                    f"Project '{name}' has no test command configured. "
                    "Runtime test validation will have nothing to run."
                ),
            ))
    return diagnostics


def collect_diagnostics() -> list[Diagnostic]:
    """
    Aggregate all checks. Each check is individually guarded so one failing
    check cannot suppress the others, and nothing here raises.
    """
    diagnostics: list[Diagnostic] = []

    for check in (check_backend_host, check_encryption_key, check_provider_keys):
        try:
            diagnostics.extend(check())
        except Exception as error:
            logger.debug("startup_diagnostics: %s skipped: %s", check.__name__, error)

    projects: list[dict] = []
    try:
        from backend.projects.project_store import list_projects
        projects = list_projects()
    except Exception as error:
        logger.debug("startup_diagnostics: project load skipped: %s", error)

    for check in (check_github_cli, check_projects):
        try:
            diagnostics.extend(check(projects))
        except Exception as error:
            logger.debug("startup_diagnostics: %s skipped: %s", check.__name__, error)

    return diagnostics


def run_startup_diagnostics() -> list[Diagnostic]:
    """
    Entry point for app startup. Log-only and exception-safe: it logs each
    finding and always returns (never raises), so it can never block startup.
    """
    try:
        diagnostics = collect_diagnostics()
    except Exception as error:
        logger.debug("startup_diagnostics: collection skipped: %s", error)
        return []

    if not diagnostics:
        logger.info("Startup diagnostics: no local setup warnings.")
        return diagnostics

    logger.warning(
        "Startup diagnostics found %d local setup warning(s):", len(diagnostics)
    )
    for diagnostic in diagnostics:
        logger.warning("[%s] %s", diagnostic.code, diagnostic.message)
    return diagnostics
