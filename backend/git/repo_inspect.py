"""
repo_inspect.py
Read-only Git repository inspection for project detection.

Everything in this module is strictly READ-ONLY. No function here checks out,
creates, stages, commits, pushes, or otherwise mutates git state. It exists so
the New Project flow can detect what kind of repo it is pointed at and suggest
a sensible pr_mode without the user filling in fields by hand.

Subprocess calls follow the same safe pattern as local_git: no shell=True,
list args only, cwd pinned to the requested path, short timeouts, and graceful
errors. Raw subprocess/OS errors are swallowed and surfaced as None/warnings so
nothing leaks to the frontend.
"""

import subprocess
from pathlib import Path

from backend.git.gh_cli import is_gh_authenticated, is_gh_installed
from backend.projects.pr_modes import PR_MODE_GITHUB_CLI, PR_MODE_LOCAL_ONLY

_GIT_TIMEOUT_SECONDS = 10


def _split_owner_repo(path: str) -> tuple[str, str] | None:
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if len(segments) < 2:
        return None
    owner = segments[0]
    repo = segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return None
    return owner, repo


def parse_github_remote(url: str | None) -> tuple[str, str] | None:
    """
    Pure parser for a GitHub remote URL.

    Supports:
      - git@github.com:owner/repo.git
      - git@github.com:owner/repo
      - https://github.com/owner/repo.git
      - https://github.com/owner/repo
      - ssh://git@github.com/owner/repo(.git)

    Returns (owner, repo) for github.com remotes, otherwise None. Non-GitHub
    and malformed URLs return None rather than raising. GitHub Enterprise hosts
    are intentionally out of scope and return None.
    """
    if not url or not str(url).strip():
        return None
    raw = str(url).strip()

    if raw.startswith("git@"):
        # scp-like syntax: git@github.com:owner/repo
        host_and_path = raw[len("git@"):]
        host, separator, path = host_and_path.partition(":")
        if not separator:
            return None
        if host.lower() != "github.com":
            return None
        return _split_owner_repo(path)

    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        if scheme.lower() not in ("https", "http", "ssh"):
            return None
        host, _, path = rest.partition("/")
        if "@" in host:
            host = host.split("@", 1)[1]
        host = host.split(":", 1)[0]
        if host.lower() != "github.com":
            return None
        return _split_owner_repo(path)

    return None


def _resolve_existing_dir(repo_path: str) -> Path | None:
    try:
        if not repo_path or not str(repo_path).strip():
            return None
        path = Path(str(repo_path).strip()).resolve()
        if not path.exists() or not path.is_dir():
            return None
        return path
    except Exception:
        return None


def _run_git_readonly(
    args: list[str],
    cwd: Path,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None


def get_git_root(repo_path: Path) -> str | None:
    """Return the absolute git root for repo_path, or None if not a repo."""
    result = _run_git_readonly(["rev-parse", "--show-toplevel"], repo_path)
    if result is None or result.returncode != 0:
        return None
    root = result.stdout.strip()
    if not root:
        return None
    try:
        return str(Path(root).resolve())
    except Exception:
        return None


def get_current_branch(repo_path: Path) -> str | None:
    """Return the current branch, or None (e.g. detached HEAD / failure)."""
    result = _run_git_readonly(["branch", "--show-current"], repo_path)
    if result is None or result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def get_origin_url(repo_path: Path) -> str | None:
    """Return the origin remote URL, or None if there is no origin."""
    result = _run_git_readonly(["remote", "get-url", "origin"], repo_path)
    if result is None or result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _paths_equal(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except Exception:
        return False


def detect_repo(repo_path: str) -> dict:
    """
    Read-only detection for the New Project flow.

    Returns a plain dict describing the repo at repo_path and a recommended
    pr_mode. This never saves project settings and never mutates git state.

    recommended_pr_mode is github_cli only when the repo has a GitHub remote
    AND gh is installed AND gh is authenticated; otherwise local_only.
    manual_token is never auto-recommended.
    """
    warnings: list[str] = []
    result = {
        "repo_path_input": repo_path,
        "is_git_repo": False,
        "git_root": None,
        "path_is_git_root": False,
        "current_branch": None,
        "origin_url": None,
        "is_github_remote": False,
        "github_owner": None,
        "github_repo": None,
        "gh_installed": False,
        "gh_authenticated": False,
        "recommended_pr_mode": PR_MODE_LOCAL_ONLY,
        "warnings": warnings,
    }

    # gh detection is independent of the target repo.
    gh_installed = is_gh_installed()
    gh_authenticated = is_gh_authenticated() if gh_installed else False
    result["gh_installed"] = gh_installed
    result["gh_authenticated"] = gh_authenticated

    resolved = _resolve_existing_dir(repo_path)
    if resolved is None:
        warnings.append("repo_path is empty or does not exist.")
        return result

    git_root = get_git_root(resolved)
    if git_root is None:
        warnings.append("Path is not a Git repository.")
        return result

    result["is_git_repo"] = True
    result["git_root"] = git_root
    result["path_is_git_root"] = _paths_equal(resolved, git_root)
    if not result["path_is_git_root"]:
        warnings.append(
            f"Path is a subdirectory of a Git repository rooted at {git_root}."
        )

    result["current_branch"] = get_current_branch(resolved)

    origin_url = get_origin_url(resolved)
    result["origin_url"] = origin_url
    if not origin_url:
        warnings.append("Repository has no 'origin' remote configured.")
    else:
        parsed = parse_github_remote(origin_url)
        if parsed is None:
            warnings.append("Repository 'origin' is not a GitHub remote.")
        else:
            owner, repo = parsed
            result["is_github_remote"] = True
            result["github_owner"] = owner
            result["github_repo"] = repo

    if result["is_github_remote"] and gh_installed and gh_authenticated:
        result["recommended_pr_mode"] = PR_MODE_GITHUB_CLI
    else:
        result["recommended_pr_mode"] = PR_MODE_LOCAL_ONLY

    return result
