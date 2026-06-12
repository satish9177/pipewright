"""
local_git.py
Safe local Git subprocess helpers for Pipewright.

All Git operations run with cwd=repo_path so Pipewright never accidentally
operates on its own repository when targeting a project repository.
"""

import subprocess
from dataclasses import dataclass
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


def branch_exists_remote(
    repo_path: str,
    branch_name: str,
    remote: str = "origin",
) -> bool:
    result = run_git(["ls-remote", "--heads", remote, branch_name], repo_path)
    if result.returncode != 0:
        raise RuntimeError(
            f"[GIT] git ls-remote failed for branch {branch_name}: "
            f"{result.stderr.strip()}"
        )
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


@dataclass(frozen=True)
class StartBranchInspection:
    current_branch: str | None = None
    head_sha: str | None = None
    head_sha_short: str | None = None
    is_detached: bool = False
    error: str | None = None


def inspect_start_branch(repo_path: str) -> StartBranchInspection:
    """
    Read the current branch + HEAD for run-creation safety checks.

    This is deliberately non-raising and read-only. It never checks out,
    creates, deletes, or mutates branches. Unknown/error states are returned as
    data so callers can avoid blocking run creation on an unverifiable repo.
    """
    try:
        branch_result = run_git(["branch", "--show-current"], repo_path)
        if branch_result.returncode != 0:
            return StartBranchInspection(
                error=(
                    "git branch --show-current failed: "
                    f"{branch_result.stderr.strip()}"
                )
            )

        head_result = run_git(["rev-parse", "HEAD"], repo_path)
        if head_result.returncode != 0:
            return StartBranchInspection(
                error=(
                    "git rev-parse HEAD failed: "
                    f"{head_result.stderr.strip()}"
                )
            )

        branch = branch_result.stdout.strip() or None
        head_sha = head_result.stdout.strip() or None
        if head_sha is None:
            return StartBranchInspection(
                current_branch=branch,
                is_detached=branch is None,
                error="git rev-parse HEAD returned empty hash",
            )
        return StartBranchInspection(
            current_branch=branch,
            head_sha=head_sha,
            head_sha_short=head_sha[:12],
            is_detached=branch is None,
        )
    except Exception as error:
        return StartBranchInspection(error=f"start branch inspection failed: {error}")


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
        f"[GIT] working tree is dirty: {', '.join(dirty_files)}. "
        "Review changes, then manually run git restore . and git clean -fd if safe."
    )


@dataclass(frozen=True)
class WorktreeStatus:
    """
    Read-only classification of a repo's working tree (#32E interruption
    guidance). `state` is one of:

      "clean"        - git repo with no uncommitted changes
      "dirty"        - git repo with uncommitted changes (see dirty_files)
      "missing_path" - repo_path is empty, does not exist, or is not a directory
      "not_a_repo"   - path exists but is not inside a git work tree
      "error"        - inspection could not complete (git missing, timeout, etc.)
    """
    state: str
    dirty_files: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_dirty(self) -> bool:
        return self.state == "dirty"


def detect_uncommitted_changes(repo_path: str) -> WorktreeStatus:
    """
    Inspect whether repo_path has uncommitted changes — strictly read-only.

    Runs only `git rev-parse` / `git status` (never reset/stash/checkout/clean/
    commit), never mutates the repo, never raises (unexpected problems are
    returned as state="error"), and safely handles missing and non-git paths.

    Intended for interruption guidance: detection only. Callers must not use it
    to drive any automatic Git mutation.
    """
    try:
        if not repo_path or not str(repo_path).strip():
            return WorktreeStatus("missing_path", reason="repo_path is empty")

        path = Path(repo_path)
        if not path.exists():
            return WorktreeStatus(
                "missing_path", reason=f"repo_path does not exist: {repo_path}"
            )
        if not path.is_dir():
            return WorktreeStatus(
                "missing_path",
                reason=f"repo_path is not a directory: {repo_path}",
            )

        probe = run_git(["rev-parse", "--is-inside-work-tree"], repo_path)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return WorktreeStatus(
                "not_a_repo",
                reason=f"repo_path is not a git repository: {repo_path}",
            )

        status = run_git(["status", "--porcelain", "-uall"], repo_path)
        if status.returncode != 0:
            return WorktreeStatus(
                "error", reason=f"git status failed: {status.stderr.strip()}"
            )

        dirty_files = get_dirty_files(repo_path)
        if dirty_files:
            return WorktreeStatus("dirty", dirty_files=tuple(dirty_files))
        return WorktreeStatus("clean")
    except Exception as error:
        return WorktreeStatus(
            "error", reason=f"worktree inspection failed: {error}"
        )


def assert_not_on_stale_pipewright_branch(
    repo_path: str,
    current_run_id: str,
) -> None:
    """
    Block fresh execution from starting on an old pipewright/* branch.

    This guard intentionally does not auto-checkout, delete, clean, or guess a
    base branch. Operators must move the target repo to the branch they want
    Pipewright to start from before starting a new run.
    """
    if not current_run_id or not current_run_id.strip():
        raise RuntimeError("[GIT] current_run_id is required")

    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    if result.returncode != 0:
        raise RuntimeError(
            f"[GIT] failed to determine current branch: {result.stderr.strip()}"
        )

    current_branch = result.stdout.strip()
    if not current_branch or current_branch == "HEAD":
        return
    if not current_branch.startswith("pipewright/"):
        return

    expected_branch = f"pipewright/{current_run_id[:8]}"
    if current_branch == expected_branch:
        return

    raise RuntimeError(
        f"[GIT] Target repo is on stale Pipewright branch '{current_branch}'. "
        f"Expected branch for this run is '{expected_branch}'. Checkout the "
        "branch you want Pipewright to start from before starting a new run."
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
        # git prints "nothing to commit, working tree clean" to stdout, not
        # stderr, so fall back to stdout (and then a generic message) to avoid
        # surfacing an empty "git commit failed:" error.
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "unknown git commit error"
        )
        raise RuntimeError(f"[GIT] git commit failed: {detail}")

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


def commits_ahead(base_branch: str, branch_name: str, repo_path: str) -> int:
    if not base_branch or not base_branch.strip():
        raise RuntimeError("[GIT] base branch is required")
    if not branch_name or not branch_name.strip():
        raise RuntimeError("[GIT] branch name is required")

    result = run_git(
        ["rev-list", "--count", f"{base_branch.strip()}..{branch_name.strip()}"],
        repo_path,
    )
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git rev-list failed: {result.stderr.strip()}")

    try:
        return int(result.stdout.strip())
    except ValueError as error:
        raise RuntimeError(
            f"[GIT] git rev-list returned invalid count: {result.stdout.strip()}"
        ) from error


def get_remote_url(repo_path: str, remote: str = "origin") -> str:
    result = run_git(["remote", "get-url", remote], repo_path)
    if result.returncode != 0:
        raise RuntimeError(
            f"[GIT] git remote get-url failed for {remote}: "
            f"{result.stderr.strip()}"
        )

    remote_url = result.stdout.strip()
    if not remote_url:
        raise RuntimeError(f"[GIT] git remote {remote} URL is empty")
    return remote_url


def checkout_file(file_path: str, repo_path: str) -> None:
    normalized_path = normalize_git_path(file_path)
    result = run_git(["checkout", "--", normalized_path], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git checkout file failed: {result.stderr.strip()}")


def commit_message_exists(repo_path: str, message_prefix: str) -> bool:
    """
    Return True if git log contains a commit message with message_prefix.
    """
    if not message_prefix or not message_prefix.strip():
        raise RuntimeError("[GIT] commit message prefix is required")

    result = run_git(["log", "--oneline"], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git log failed: {result.stderr.strip()}")

    prefix = message_prefix.strip()
    return any(prefix in line for line in result.stdout.splitlines())


def diff_range(base_ref: str, repo_path: str, head_ref: str = "HEAD") -> str:
    """
    Return the textual diff of head_ref against base_ref (``git diff base..head``).

    Read-only: never mutates the worktree, index, or refs. Used by the
    final-approval summary (item 14) to show the cumulative branch diff. Raises
    on a git error so the caller can fall back to a clear "diff unavailable" note
    rather than silently showing an empty diff.
    """
    if not base_ref or not base_ref.strip():
        raise RuntimeError("[GIT] diff_range requires a base ref")
    result = run_git(["diff", f"{base_ref.strip()}..{head_ref}"], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git diff range failed: {result.stderr.strip()}")
    return result.stdout


def show_commit(commit_ref: str, repo_path: str) -> str:
    """
    Return ``git show`` (patch form) for a single commit. Read-only.

    Used by item 14 to carry a completed chunk's committed diff into the
    refinement continuation context as text. Raises on a git error.
    """
    if not commit_ref or not commit_ref.strip():
        raise RuntimeError("[GIT] show_commit requires a commit ref")
    result = run_git(["show", commit_ref.strip()], repo_path)
    if result.returncode != 0:
        raise RuntimeError(f"[GIT] git show failed: {result.stderr.strip()}")
    return result.stdout
