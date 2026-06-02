"""
pr_preflight.py
Targeted pre-PR safety checks for the remote GitHub PR modes (#20B-2).

This is intentionally narrow: it does NOT fetch, compare commits against the
remote base, auto-create or auto-push any branch, or implement a broad preflight
framework. It only answers the one question that the github_cli / manual_token
flows previously never asked — does the chosen base branch actually exist on the
remote? — so a missing remote base fails with a clear, actionable recovery
message instead of an opaque GitHub GraphQL error ("Base ref must be a branch",
"Base sha can't be blank", "No commits between …").

local_only mode never reaches this module.
"""

from backend.git import local_git
from backend.llm.sanitize import sanitize_for_log


class PrPreflightError(RuntimeError):
    """
    A pre-PR check failed. Raised before any push / gh pr create / create_pull,
    so the opaque GitHub error is never reached. The orchestrator's existing
    handler sanitizes this, marks the run push_failed, and surfaces it as the
    HTTP 400 detail / push_error.
    """

    def __init__(
        self,
        *,
        failure_type: str,
        message: str,
        recovery_hint: str = "",
    ):
        self.failure_type = failure_type
        self.recovery_hint = recovery_hint
        full = f"{message} {recovery_hint}".strip() if recovery_hint else message
        super().__init__(full)


def ensure_remote_base_branch(
    repo_path: str,
    base_branch: str,
    remote: str = "origin",
) -> None:
    """
    Verify ``base_branch`` exists on ``remote`` (``git ls-remote --heads``).

    Reuses ``local_git.branch_exists_remote`` (list-args subprocess, no shell).
    Raises ``PrPreflightError`` when the base is missing (REMOTE_BASE_MISSING)
    or the git probe itself fails (GIT_COMMAND_FAILED). Never pushes or creates
    anything.
    """
    try:
        exists = local_git.branch_exists_remote(repo_path, base_branch, remote)
    except Exception as error:
        raise PrPreflightError(
            failure_type="GIT_COMMAND_FAILED",
            message=(
                f"Could not verify base branch '{base_branch}' on '{remote}': "
                f"{sanitize_for_log(str(error))}"
            ),
        )

    if not exists:
        raise PrPreflightError(
            failure_type="REMOTE_BASE_MISSING",
            message=f"Base branch '{base_branch}' is not on '{remote}'.",
            recovery_hint=f"Push it with: git push -u {remote} {base_branch}",
        )
