"""
Tests for log-only local-first startup diagnostics (#32B).

These verify that diagnostics warn (never raise) for common local setup gaps,
that the non-loopback exposure warning fires correctly, and that no secret
value (encryption key) leaks into a message. Also asserts the docs scrub: no
runnable example still binds the backend to 0.0.0.0.
"""

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from backend.config.keys import settings
from backend.git import gh_cli
from backend.runtime import startup_diagnostics
from backend.runtime.startup_diagnostics import (
    check_backend_host,
    check_encryption_key,
    check_github_cli,
    check_projects,
    check_provider_keys,
    run_startup_diagnostics,
)

pytestmark = pytest.mark.unit


def _codes(diagnostics):
    return {d.code for d in diagnostics}


# --- backend host (non-loopback exposure fence) ---------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com", "192.168.1.10"])
def test_non_loopback_host_warns(host):
    diagnostics = check_backend_host(host)
    assert _codes(diagnostics) == {"non_loopback_host"}
    # The configured host is surfaced so the operator can see what is exposed.
    assert host in diagnostics[0].message


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "LOCALHOST", ""])
def test_loopback_host_no_warning(host):
    assert check_backend_host(host) == []


def test_host_default_unset_is_safe(monkeypatch):
    monkeypatch.delenv(startup_diagnostics.BACKEND_HOST_ENV, raising=False)
    assert check_backend_host() == []


# --- encryption key -------------------------------------------------------

def test_encryption_key_missing_warns():
    diagnostics = check_encryption_key(key=None)
    assert _codes(diagnostics) == {"encryption_key_missing"}


def test_encryption_key_invalid_warns_and_does_not_leak():
    bad_key = "this-is-not-a-valid-fernet-key-value"
    diagnostics = check_encryption_key(key=bad_key)
    assert _codes(diagnostics) == {"encryption_key_invalid"}
    # The bad key value must never appear in the warning message.
    assert bad_key not in diagnostics[0].message


def test_encryption_key_valid_no_warning():
    valid_key = Fernet.generate_key().decode("utf-8")
    assert check_encryption_key(key=valid_key) == []


# --- provider keys --------------------------------------------------------

def test_provider_key_missing_warns_without_value(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    diagnostics = check_provider_keys(["openai"])
    assert _codes(diagnostics) == {"provider_key_missing"}
    assert "OPENAI_API_KEY" in diagnostics[0].message


def test_provider_key_present_no_warning(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-value")
    assert check_provider_keys(["openai"]) == []


def test_unknown_provider_skipped():
    assert check_provider_keys(["some-custom-provider"]) == []


# --- github_cli mode ------------------------------------------------------

def test_github_cli_not_installed_warns(monkeypatch):
    monkeypatch.setattr(gh_cli, "is_gh_installed", lambda: False)
    projects = [{"name": "p", "pr_mode": "github_cli"}]
    assert _codes(check_github_cli(projects)) == {"gh_not_installed"}


def test_github_cli_unauthenticated_warns(monkeypatch):
    monkeypatch.setattr(gh_cli, "is_gh_installed", lambda: True)
    monkeypatch.setattr(gh_cli, "is_gh_authenticated", lambda: False)
    projects = [{"name": "p", "pr_mode": "github_cli"}]
    assert _codes(check_github_cli(projects)) == {"gh_not_authenticated"}


def test_github_cli_not_used_skips_gh_check(monkeypatch):
    # gh must not even be probed when no project uses github_cli mode.
    def _boom():
        raise AssertionError("gh should not be checked for local_only projects")

    monkeypatch.setattr(gh_cli, "is_gh_installed", _boom)
    projects = [{"name": "p", "pr_mode": "local_only"}]
    assert check_github_cli(projects) == []


# --- project repo/test-command checks ------------------------------------

def test_project_repo_path_missing_warns():
    projects = [{"name": "p", "repo_path": "", "test_command": "pytest"}]
    assert "repo_path_missing" in _codes(check_projects(projects))


def test_project_repo_path_not_found_warns():
    projects = [{
        "name": "p",
        "repo_path": "/definitely/not/a/real/path/xyz123",
        "test_command": "pytest",
    }]
    assert "repo_path_not_found" in _codes(check_projects(projects))


def test_project_repo_path_not_git_warns(tmp_path):
    projects = [{"name": "p", "repo_path": str(tmp_path), "test_command": "pytest"}]
    assert "repo_path_not_git" in _codes(check_projects(projects))


def test_project_with_git_repo_no_repo_warning(tmp_path):
    (tmp_path / ".git").mkdir()
    projects = [{"name": "p", "repo_path": str(tmp_path), "test_command": "pytest"}]
    codes = _codes(check_projects(projects))
    assert "repo_path_not_git" not in codes
    assert "repo_path_not_found" not in codes


def test_project_missing_test_command_warns(tmp_path):
    (tmp_path / ".git").mkdir()
    projects = [{"name": "p", "repo_path": str(tmp_path), "test_command": ""}]
    assert "test_command_missing" in _codes(check_projects(projects))


# --- top-level entry point ------------------------------------------------

def test_run_startup_diagnostics_never_raises_and_returns_list():
    # Smoke: must be log-only and exception-safe regardless of local env state.
    result = run_startup_diagnostics()
    assert isinstance(result, list)


# --- docs scrub regression ------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The design audit references "--host 0.0.0.0" descriptively when explaining the
# finding; it is not a runnable instruction, so it is the one allowed exception.
_DESIGN_DOC = _REPO_ROOT / "docs" / "design" / "local-first-hardening.md"


def _doc_paths():
    # Real documentation only: the docs/ tree plus top-level markdown
    # (README.md, DECISIONS.md). backend/backups/** holds immutable snapshots of
    # other repos' files and is intentionally excluded.
    paths = list((_REPO_ROOT / "docs").rglob("*.md"))
    paths += list(_REPO_ROOT.glob("*.md"))
    return paths


def test_no_unsafe_host_examples_in_docs():
    offenders = []
    for md_path in _doc_paths():
        if md_path.resolve() == _DESIGN_DOC.resolve():
            continue
        text = md_path.read_text(encoding="utf-8", errors="ignore")
        if "--host 0.0.0.0" in text:
            offenders.append(str(md_path.relative_to(_REPO_ROOT)))
    assert offenders == [], f"Unsafe 0.0.0.0 bind examples remain: {offenders}"
