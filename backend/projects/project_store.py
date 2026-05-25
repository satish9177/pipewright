"""
project_store.py
CRUD helpers for target projects.
Projects remove the need to edit .env for repo path and test command.
"""

import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.security.secrets import encrypt_secret


logger = logging.getLogger(__name__)


def _new_project_id() -> str:
    return f"proj-{uuid.uuid4().hex[:8]}"


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row._mapping)


def create_project(
    name: str,
    repo_path: str,
    test_command: str,
    branch: str = "main",
    description: str = "",
    github_token: str | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
    github_base_branch: str = "pipewright-staging",
) -> dict:
    try:
        if not name or not name.strip():
            raise ValueError("project_store.py: project name is required")
        if not repo_path or not repo_path.strip():
            raise ValueError("project_store.py: repo_path is required")
        if not test_command or not test_command.strip():
            raise ValueError("project_store.py: test_command is required")

        project_id = _new_project_id()
        normalized_repo_path = str(Path(repo_path.strip()))
        normalized_branch = branch.strip() if branch and branch.strip() else "main"
        normalized_base_branch = (
            github_base_branch.strip()
            if github_base_branch and github_base_branch.strip()
            else "pipewright-staging"
        )
        now = datetime.now(timezone.utc).isoformat()

        encrypted_github_token = encrypt_secret(github_token)

        init_db()
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO projects
                (
                    id, name, repo_path, test_command, branch, description,
                    github_token, github_owner, github_repo,
                    github_base_branch, status, created_at
                )
                VALUES
                (
                    :id, :name, :repo_path, :test_command, :branch,
                    :description, :github_token, :github_owner,
                    :github_repo, :github_base_branch, 'active', :created_at
                )
            """), {
                "id": project_id,
                "name": name.strip(),
                "repo_path": normalized_repo_path,
                "test_command": test_command.strip(),
                "branch": normalized_branch,
                "description": description.strip() if description else "",
                "github_token": encrypted_github_token,
                "github_owner": github_owner,
                "github_repo": github_repo,
                "github_base_branch": normalized_base_branch,
                "created_at": now,
            })
            conn.commit()

        return get_project(project_id)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"project_store.py: failed to create project: {error}")


def get_project(project_id: str) -> dict | None:
    try:
        init_db()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM projects
                WHERE id = :id
            """), {"id": project_id}).fetchone()
        return _row_to_dict(row)
    except Exception as error:
        raise RuntimeError(
            f"project_store.py: failed to load project. "
            f"project_id={project_id} | error={error}"
        )


def list_projects() -> list[dict]:
    try:
        init_db()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM projects
                ORDER BY created_at DESC
            """)).fetchall()
        return [dict(row._mapping) for row in rows]
    except Exception as error:
        raise RuntimeError(f"project_store.py: failed to list projects: {error}")


def update_project(
    project_id: str,
    name: str = None,
    test_command: str = None,
    branch: str = None,
    description: str = None,
    github_token: str = None,
    github_owner: str = None,
    github_repo: str = None,
    github_base_branch: str = None
) -> dict | None:
    """
    Update project fields.
    Only updates fields that are not None.
    Supports GitHub credential updates.
    Returns updated project or None if not found.
    """
    project = get_project(project_id)
    if not project:
        return None

    now = datetime.now(timezone.utc).isoformat()

    updated = {
        "name": name.strip() if name else project["name"],
        "test_command": test_command.strip() if test_command else project["test_command"],
        "branch": branch.strip() if branch else project["branch"],
        "description": description.strip() if description is not None else project.get("description", ""),
        "github_token": encrypt_secret(github_token) if github_token is not None else project.get("github_token"),
        "github_owner": github_owner if github_owner is not None else project.get("github_owner"),
        "github_repo": github_repo if github_repo is not None else project.get("github_repo"),
        "github_base_branch": github_base_branch if github_base_branch is not None else project.get("github_base_branch", "pipewright-staging"),
        "updated_at": now,
        "id": project_id
    }

    try:
        with engine.connect() as conn:
            conn.execute(text("""
                UPDATE projects
                SET name = :name,
                    test_command = :test_command,
                    branch = :branch,
                    description = :description,
                    github_token = :github_token,
                    github_owner = :github_owner,
                    github_repo = :github_repo,
                    github_base_branch = :github_base_branch,
                    updated_at = :updated_at
                WHERE id = :id
            """), updated)
            conn.commit()
        logger.info("[PROJECTS] Updated project | id=%s", project_id)
        return get_project(project_id)
    except Exception as e:
        raise RuntimeError(
            f"project_store.py: update_project failed: {e}"
        )


def require_project(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise ValueError(f"project_store.py: project not found: {project_id}")
    return project
