"""
gh_pr.py
GitHub CLI (`gh`) pull request operations for the github_cli PR mode.

This module is only ever invoked AFTER final human approval, from
pr_orchestrator. It deliberately does NOT merge, force-push, delete branches,
or poll CI. It can:

  - confirm gh is installed and authenticated (fail safely otherwise),
  - find an existing open PR for a head/base pair and reuse it,
  - create a new PR when none exists.

Subprocess rules mirror the rest of the codebase: no shell=True, list args
only, cwd pinned to the project repo, a short timeout, and every gh error is
sanitized before it is surfaced or stored so no token or credential leaks.
"""

import json
import subprocess

from backend.git.gh_cli import is_gh_authenticated, is_gh_installed
from backend.llm.sanitize import sanitize_for_log

_GH_TIMEOUT_SECONDS = 30

GH_NOT_READY_MESSAGE = (
    "GitHub CLI is selected, but gh is not installed or not authenticated. "
    "Run `gh auth login`, then retry PR creation."
)


class GhCliError(RuntimeError):
    """A gh CLI operation failed. The message is already sanitized."""


def ensure_gh_ready() -> None:
    """
    Fail safely if gh cannot be used for a PR.

    Raises GhCliError with a clear, actionable message when gh is missing or
    not authenticated. Callers must invoke this before any push so a
    misconfigured github_cli project never silently falls through.
    """
    if not is_gh_installed() or not is_gh_authenticated():
        raise GhCliError(GH_NOT_READY_MESSAGE)


def _run_gh(
    args: list[str],
    repo_path: str,
    timeout: int = _GH_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as error:
        raise GhCliError(
            f"gh_pr.py: gh command failed: {sanitize_for_log(str(error))}"
        )


def find_open_pr(
    repo_path: str,
    branch_name: str,
    base_branch: str,
) -> dict | None:
    """
    Return the first open PR for head=branch_name, base=base_branch, or None.

    Uses `gh pr list --head <branch> --base <base> --state open --json
    url,number,title`. Returns a dict with url, number, title.
    """
    result = _run_gh(
        [
            "pr", "list",
            "--head", branch_name,
            "--base", base_branch,
            "--state", "open",
            "--json", "url,number,title",
        ],
        repo_path,
    )
    if result.returncode != 0:
        raise GhCliError(
            f"gh_pr.py: gh pr list failed: "
            f"{sanitize_for_log((result.stderr or '').strip())}"
        )

    raw = (result.stdout or "").strip()
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        # gh returns a JSON array; anything else means no usable PR data.
        return None
    if not items:
        return None

    first = items[0]
    number = first.get("number")
    url = first.get("url")
    if number is None or not url:
        return None
    return {
        "url": url,
        "number": int(number),
        "title": first.get("title"),
    }


def get_pr_checks(
    repo_path: str,
    identifier: str | int,
    timeout: int = _GH_TIMEOUT_SECONDS,
) -> list[dict]:
    """
    Return the raw per-check rows for a PR via `gh pr checks ... --json`.

    Read-only: this never merges, comments, re-runs, or mutates anything, and it
    is only ever called by an explicit checks caller — never on a normal read.

    `gh pr checks` uses its exit code to signal check *results* (it exits
    non-zero when checks are failing or pending), so the exit code is NOT treated
    as a fetch error: the JSON on stdout is authoritative and is parsed
    regardless of return code. A missing PR / no configured checks yields an
    empty list. Any genuine CLI problem (no JSON, unparseable output) raises
    GhCliError so the caller can report the checks as *unavailable*, never as
    failed. The returned rows carry only gh's summary fields — no raw logs.
    """
    result = _run_gh(
        [
            "pr", "checks", str(identifier),
            "--json", "name,state,bucket,workflow",
        ],
        repo_path,
        timeout=timeout,
    )

    raw = (result.stdout or "").strip()
    if not raw:
        stderr = (result.stderr or "").lower()
        if "no check" in stderr:
            # gh reports "no checks reported on the '<branch>' branch".
            return []
        raise GhCliError(
            f"gh_pr.py: gh pr checks returned no data: "
            f"{sanitize_for_log((result.stderr or '').strip())}"
        )

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        raise GhCliError("gh_pr.py: gh pr checks output was not valid JSON.")

    if not isinstance(items, list):
        return []
    return items


def _extract_pr_url(stdout: str | None) -> str:
    """Pull the PR URL out of `gh pr create` stdout (last /pull/ line)."""
    for line in reversed((stdout or "").splitlines()):
        candidate = line.strip()
        if "/pull/" in candidate:
            return candidate
    return (stdout or "").strip()


def _parse_pr_number(url: str) -> int | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def create_pr(
    repo_path: str,
    branch_name: str,
    base_branch: str,
    title: str,
    body: str,
) -> dict:
    """
    Create a PR with gh and return its url, number, and title.

    Uses `gh pr create --base <base> --head <branch> --title <title>
    --body <body>`. gh prints the new PR URL to stdout; the number is parsed
    from that URL.
    """
    result = _run_gh(
        [
            "pr", "create",
            "--base", base_branch,
            "--head", branch_name,
            "--title", title,
            "--body", body,
        ],
        repo_path,
    )
    if result.returncode != 0:
        raise GhCliError(
            f"gh_pr.py: gh pr create failed: "
            f"{sanitize_for_log((result.stderr or '').strip())}"
        )

    url = _extract_pr_url(result.stdout)
    number = _parse_pr_number(url)
    if not url or number is None:
        raise GhCliError(
            "gh_pr.py: PR was created but its URL/number could not be parsed "
            "from gh output."
        )
    return {"url": url, "number": number, "title": title}
