"""
local_git.py
Safe local Git subprocess helpers for Pipewright.

All Git operations run with cwd=repo_path so Pipewright never accidentally
operates on its own repository when targeting a project repository.
"""

import subprocess
from pathlib import Path


def _validate_repo_path(repo_path: str) -> Path:
    try:
        if not repo_path or not str(repo_path).strip():
            raise RuntimeError("[GIT] repo_path is required")

        path = Path(repo_path).resolve()
        if not path.exists():
            raise RuntimeError(f"[GIT] repo_path does not exist: {repo_path}")
        if not path.is_dir():
            raise RuntimeError(f"[GIT] repo_path is not a directory: {repo_path}")
        return path
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"[GIT] failed to validate repo_path: {error}")


def normalize_git_path(path: str) -> str:
    """
    Normalize and validate a git path.

    Rejects empty, absolute, and traversal paths. Always returns forward slashes.
    """
    try:
        if path is None:
            raise ValueError("[SECURITY] Unsafe git path rejected: empty path")

        normalized = str(path).strip().replace("\\", "/")
        if not normalized:
            raise ValueError("[SECURITY] Unsafe git path rejected: empty path")
        if normalized.startswith("/") or normalized.startswith("\\"):
            raise ValueError(f"[SECURITY] Unsafe git path rejected: {path}")
        if Path(normalized).is_absolute():
            raise ValueError(f"[SECURITY] Unsafe git path rejected: {path}")
        if ".." in normalized.split("/"):
            raise ValueError(f"[SECURITY] Unsafe git path rejected: {path}")

        return normalized
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"[SECURITY] Unsafe git path rejected: {path} | {error}")


def run_git(
    args: list[str],
    repo_path: str,
    timeout: int = 30
) -> subprocess.CompletedProcess:
    """
    Run a git command inside repo_path.
    args must not include "git" itself.
    """
    try:
        if not args:
            raise RuntimeError("[GIT] git args cannot be empty")
        if args[0] == "git":
            raise RuntimeError("[GIT] args must not include git")

        repo = _validate_repo_path(repo_path)
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"[GIT] git subprocess execution failed: {error}")


def ensure_git_repo(repo_path: str) -> None:
    repo = _validate_repo_path(repo_path)

    result = run_git(["rev-parse", "--is-inside-work-tree"], repo_path)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError(
            f"[GIT] repo_path is not a git repository: {repo_path} "
            f"{result.stderr.strip()}"
        )

    top_level = run_git(["rev-parse", "--show-toplevel"], repo_path)
    if top_level.returncode != 0:
        raise RuntimeError(
            f"[GIT] failed to resolve git repository root: "
            f"{top_level.stderr.strip()}"
        )

    git_root = Path(top_level.stdout.strip()).resolve()
    if git_root != repo:
        raise RuntimeError(
            f"[GIT] repo_path is inside a Git worktree but is not the repo root: "
            f"repo_path={repo} git_root={git_root}"
        )


def get_current_hash(repo_path: str) -> str:
    result = run_git(["rev-parse", "HEAD"], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git rev-parse HEAD failed: {result.stderr.strip()}")

    git_hash = result.stdout.strip()
    if not git_hash:
        raise RuntimeError("[GIT] git rev-parse HEAD returned empty hash")
    return git_hash


def branch_exists(branch_name: str, repo_path: str) -> bool:
    result = run_git(["branch", "--list", branch_name], repo_path)
    return result.stdout.strip() != ""


def create_or_checkout_branch(branch_name: str, repo_path: str) -> None:
    ensure_git_repo(repo_path)

    if branch_exists(branch_name, repo_path):
        result = run_git(["checkout", branch_name], repo_path)
    else:
        result = run_git(["checkout", "-b", branch_name], repo_path)

    if result.returncode != 0:
        raise RuntimeError(
            f"[GIT] git checkout failed for branch {branch_name}: "
            f"{result.stderr.strip()}"
        )


def get_current_branch(repo_path: str) -> str:
    result = run_git(["branch", "--show-current"], repo_path)
    if result.returncode != 0:
        raise RuntimeError(
            f"[GIT] git branch --show-current failed: {result.stderr.strip()}"
        )

    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("[GIT] current branch is empty")
    return branch


def is_working_tree_clean(repo_path: str) -> bool:
    result = run_git(["status", "--porcelain", "-uall"], repo_path)
    return result.stdout.strip() == ""


def get_dirty_files(repo_path: str) -> list[str]:
    result = run_git(["status", "--porcelain", "-uall"], repo_path)
    dirty_files: list[str] = []

    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue

        path_part = line[3:] if len(line) > 3 else ""
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]

        normalized = path_part.strip().replace("\\", "/").strip('"')
        if normalized:
            dirty_files.append(normalized)

    return dirty_files


def ensure_clean_worktree(repo_path: str) -> None:
    if is_working_tree_clean(repo_path):
        return

    dirty_files = get_dirty_files(repo_path)
    raise RuntimeError(
        f"[GIT] working tree is dirty: {', '.join(dirty_files)}"
    )


def stage_files(file_paths: list[str], repo_path: str) -> None:
    if not file_paths:
        raise RuntimeError("[GIT] stage_files requires at least one file")

    normalized_paths = [normalize_git_path(path) for path in file_paths]
    result = run_git(["add", "--", *normalized_paths], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git add failed: {result.stderr.strip()}")


def commit(message: str, repo_path: str) -> str:
    if not message or not message.strip():
        raise RuntimeError("[GIT] commit message is required")

    result = run_git(["commit", "-m", message.strip()], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git commit failed: {result.stderr.strip()}")

    return get_current_hash(repo_path)


def commit_files(file_paths: list[str], message: str, repo_path: str) -> str:
    try:
        stage_files(file_paths, repo_path)
        return commit(message, repo_path)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"[GIT] commit_files failed: {error}")


def push_branch(
    branch_name: str,
    repo_path: str,
    remote: str = "origin"
) -> None:
    result = run_git(["push", "-u", remote, branch_name], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git push failed: {result.stderr.strip()}")


def checkout_file(file_path: str, repo_path: str) -> None:
    normalized_path = normalize_git_path(file_path)
    result = run_git(["checkout", "--", normalized_path], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git checkout file failed: {result.stderr.strip()}")
