"""
Verify stored Pipewright project configuration without printing secrets.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.git import local_git
from backend.projects.project_store import get_project


DEFAULT_PROJECT_ID = "proj-13605886"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a Pipewright project")
    parser.add_argument(
        "project_id",
        nargs="?",
        default=DEFAULT_PROJECT_ID,
        help=f"Project id, default {DEFAULT_PROJECT_ID}",
    )
    return parser.parse_args()


def remote_matches(remote_url: str, owner: str | None, repo_name: str | None) -> bool:
    if not owner or not repo_name:
        return True
    normalized = remote_url.strip().lower().removesuffix(".git")
    expected = f"{owner}/{repo_name}".lower()
    if normalized.startswith("git@github.com:"):
        return normalized.split("git@github.com:", 1)[1] == expected
    if "github.com/" in normalized:
        return normalized.split("github.com/", 1)[1] == expected
    return False


def main() -> int:
    args = parse_args()
    project = get_project(args.project_id)
    if project is None:
        print(f"[ERROR] Project not found: {args.project_id}")
        return 1

    safe_fields = [
        "id",
        "name",
        "repo_path",
        "test_command",
        "branch",
        "github_owner",
        "github_repo",
        "github_base_branch",
    ]
    for field in safe_fields:
        print(f"{field}: {project.get(field)}")
    print(f"has_github_token: {bool(project.get('github_token'))}")

    repo_path = project.get("repo_path")
    repo = Path(repo_path) if repo_path else None
    if not repo or not repo.exists():
        print(f"[WARN] repo_path does not exist: {repo_path}")
        return 0
    if not repo.is_dir():
        print(f"[WARN] repo_path is not a directory: {repo_path}")
        return 0

    try:
        local_git.ensure_git_repo(str(repo))
        print("[OK] repo_path is a git root")
    except Exception as error:
        print(f"[WARN] repo_path is not a git root: {error}")
        return 0

    try:
        print(f"current_branch: {local_git.get_current_branch(str(repo))}")
    except Exception as error:
        print(f"[WARN] could not read current branch: {error}")

    try:
        remote_url = local_git.get_remote_url(str(repo))
        print(f"origin: {remote_url}")
        if remote_matches(
            remote_url,
            project.get("github_owner"),
            project.get("github_repo"),
        ):
            print("[OK] origin matches configured GitHub repo")
        else:
            print("[WARN] origin does not match configured GitHub repo")
    except Exception as error:
        print(f"[WARN] could not read origin remote: {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
