"""
branch_safety.py
Shared base-branch safety validation for every automated GitHub PR path.

Both the legacy github_client and the modern chunked pr_orchestrator import
this single source of truth so a forbidden base branch (main, master,
develop) can never be targeted by an automated push or PR, and a missing
configuration never silently falls back to main.
"""

DEFAULT_BASE_BRANCH = "pipewright-staging"
FORBIDDEN_BASE_BRANCHES = {"main", "master", "develop"}


def validate_base_branch(github_base_branch: str | None) -> str:
    """
    Resolve and validate the base branch for an automated PR.

    - Missing/empty -> safe default (pipewright-staging). Never main.
    - A forbidden base branch raises RuntimeError so no push or PR happens.
    """
    base_branch = (github_base_branch or "").strip() or DEFAULT_BASE_BRANCH
    if base_branch in FORBIDDEN_BASE_BRANCHES:
        raise RuntimeError(
            f"branch_safety.py: forbidden base branch: {base_branch}. "
            f"Configure github_base_branch to a non-protected branch "
            f"(default: {DEFAULT_BASE_BRANCH}). No push or PR was created."
        )
    return base_branch
