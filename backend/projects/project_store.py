"""
project_store.py
CRUD helpers for target projects.
Projects remove the need to edit .env for repo path and test command.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from backend.db.database import engine, init_db


def _new_project_id() -> str:
    return f"proj-{uuid.uuid4().hex[:8]}"


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row._mapping)


def create_project(name: str, repo_path: str, test_command: str) -> dict:
    try:
        if not name or not name.strip():
            raise ValueError("project_store.py: project name is required")
        if not repo_path or not repo_path.strip():
            raise ValueError("project_store.py: repo_path is required")
        if not test_command or not test_command.strip():
            raise ValueError("project_store.py: test_command is required")

        project_id = _new_project_id()
        normalized_repo_path = str(Path(repo_path.strip()))
        now = datetime.now(timezone.utc).isoformat()

        init_db()
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO projects
                (id, name, repo_path, test_command, status, created_at)
                VALUES
                (:id, :name, :repo_path, :test_command, 'active', :created_at)
            """), {
                "id": project_id,
                "name": name.strip(),
                "repo_path": normalized_repo_path,
                "test_command": test_command.strip(),
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


def require_project(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise ValueError(f"project_store.py: project not found: {project_id}")
    return project
