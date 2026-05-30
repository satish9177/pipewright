"""
gh_cli.py
Read-only detection of the GitHub CLI (`gh`).

This module ONLY answers two questions:
  - is gh installed?
  - is gh authenticated?

It intentionally does NOT implement `gh pr create`, `gh pr list`, `gh repo`,
push logic, or any other GitHub mutation. Those are future work. Subprocess
calls use list args (never shell=True), short timeouts, and never log the
output of `gh auth status` so no token or auth detail can leak to logs.
"""

import shutil
import subprocess

_GH_TIMEOUT_SECONDS = 10


def is_gh_installed() -> bool:
    """Return True if the gh executable is on PATH."""
    try:
        return shutil.which("gh") is not None
    except Exception:
        return False


def is_gh_authenticated() -> bool:
    """
    Return True if `gh auth status` exits 0.

    Returns False if gh is missing, not authenticated, times out, or errors.
    The command output is never logged because it can reference accounts and
    token scopes.
    """
    if not is_gh_installed():
        return False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False
