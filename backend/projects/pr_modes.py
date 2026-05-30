"""
pr_modes.py
Single source of truth for project PR creation modes.

  - local_only   : default. Pipewright commits to a local branch only.
  - github_cli    : recommended when gh is installed and authenticated and the
                    repo has a GitHub remote. (PR creation itself is future work.)
  - manual_token  : advanced fallback using a stored github_token. Backfill mode
                    for existing projects that already configured a token.

GitHub App support is intentionally future work and is not a valid mode here.
"""

PR_MODE_LOCAL_ONLY = "local_only"
PR_MODE_GITHUB_CLI = "github_cli"
PR_MODE_MANUAL_TOKEN = "manual_token"

ALLOWED_PR_MODES = (
    PR_MODE_LOCAL_ONLY,
    PR_MODE_GITHUB_CLI,
    PR_MODE_MANUAL_TOKEN,
)

DEFAULT_PR_MODE = PR_MODE_LOCAL_ONLY


def normalize_pr_mode(value: str | None) -> str:
    """
    Validate and normalize a pr_mode value.

    Missing/empty -> DEFAULT_PR_MODE (local_only).
    Any value outside ALLOWED_PR_MODES raises ValueError.
    """
    if value is None:
        return DEFAULT_PR_MODE
    normalized = str(value).strip()
    if not normalized:
        return DEFAULT_PR_MODE
    if normalized not in ALLOWED_PR_MODES:
        raise ValueError(
            f"pr_mode must be one of {', '.join(ALLOWED_PR_MODES)}; "
            f"got: {normalized}"
        )
    return normalized
