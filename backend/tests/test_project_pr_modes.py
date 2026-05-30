"""
test_project_pr_modes.py
Tests for the project pr_mode field: data model, store defaults, migration
backfill, and response safety.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from backend.db.database import _migrate_db
from backend.models.handoff import ProjectCreate, ProjectResponse, ProjectUpdate
from backend.projects.project_store import create_project, update_project

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

def test_project_create_default_pr_mode_is_local_only():
    model = ProjectCreate(name="t", repo_path="/x/pytest_tmp", test_command="pytest")
    assert model.pr_mode == "local_only"


@pytest.mark.parametrize("mode", ["local_only", "github_cli", "manual_token"])
def test_project_create_accepts_allowed_pr_modes(mode):
    model = ProjectCreate(
        name="t",
        repo_path="/x/pytest_tmp",
        test_command="pytest",
        pr_mode=mode,
    )
    assert model.pr_mode == mode


def test_project_create_rejects_invalid_pr_mode():
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="t",
            repo_path="/x/pytest_tmp",
            test_command="pytest",
            pr_mode="bogus_mode",
        )


def test_project_update_rejects_invalid_pr_mode():
    with pytest.raises(ValidationError):
        ProjectUpdate(pr_mode="not_a_mode")


def test_project_update_allows_none_pr_mode():
    model = ProjectUpdate(name="t")
    assert model.pr_mode is None


def test_project_response_exposes_pr_mode_never_token():
    resp = ProjectResponse(
        id="proj-1",
        name="n",
        repo_path="/x/pytest_tmp",
        test_command="pytest",
        pr_mode="github_cli",
    )
    dumped = resp.model_dump()
    assert dumped["pr_mode"] == "github_cli"
    assert "github_token" not in dumped


# ---------------------------------------------------------------------------
# Store behavior (against the real app DB; cleaned up by conftest)
# ---------------------------------------------------------------------------

def test_create_project_defaults_pr_mode_local_only(tmp_repo):
    project = create_project(
        name="pr-mode-default",
        repo_path=str(tmp_repo),
        test_command="pytest",
    )
    assert project["pr_mode"] == "local_only"


def test_create_project_with_manual_token_mode(tmp_repo):
    project = create_project(
        name="pr-mode-manual",
        repo_path=str(tmp_repo),
        test_command="pytest",
        pr_mode="manual_token",
    )
    assert project["pr_mode"] == "manual_token"


def test_update_project_changes_pr_mode(tmp_repo):
    project = create_project(
        name="pr-mode-update",
        repo_path=str(tmp_repo),
        test_command="pytest",
    )
    updated = update_project(project["id"], pr_mode="github_cli")
    assert updated["pr_mode"] == "github_cli"


def test_update_project_preserves_pr_mode_when_not_provided(tmp_repo):
    project = create_project(
        name="pr-mode-preserve",
        repo_path=str(tmp_repo),
        test_command="pytest",
        pr_mode="manual_token",
    )
    updated = update_project(project["id"], name="renamed")
    assert updated["pr_mode"] == "manual_token"


# ---------------------------------------------------------------------------
# Migration backfill (isolated temp DB, exercises _migrate_db directly)
# ---------------------------------------------------------------------------

def _old_projects_db(db_path):
    """Create a pre-pr_mode projects table and return the engine."""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT,
                repo_path TEXT,
                test_command TEXT,
                github_token TEXT,
                github_owner TEXT,
                github_repo TEXT
            )
        """))
        conn.commit()
    return engine


def test_migration_adds_pr_mode_and_backfills(tmp_path):
    engine = _old_projects_db(tmp_path / "old.db")
    with engine.connect() as conn:
        # Full manual GitHub config -> manual_token
        conn.execute(text(
            "INSERT INTO projects "
            "(id, name, repo_path, test_command, github_token, github_owner, github_repo) "
            "VALUES ('p-full','n','/a','pytest','tok','owner','repo')"
        ))
        # No GitHub config -> local_only
        conn.execute(text(
            "INSERT INTO projects (id, name, repo_path, test_command) "
            "VALUES ('p-none','n','/b','pytest')"
        ))
        # Partial GitHub config (missing repo) -> local_only
        conn.execute(text(
            "INSERT INTO projects "
            "(id, name, repo_path, test_command, github_token, github_owner) "
            "VALUES ('p-partial','n','/c','pytest','tok','owner')"
        ))
        conn.commit()

        _migrate_db(conn)
        conn.commit()

        rows = conn.execute(
            text("SELECT id, pr_mode FROM projects ORDER BY id")
        ).fetchall()

    modes = {row[0]: row[1] for row in rows}
    assert modes["p-full"] == "manual_token"
    assert modes["p-none"] == "local_only"
    assert modes["p-partial"] == "local_only"


def test_migration_is_idempotent_and_preserves_choices(tmp_path):
    engine = _old_projects_db(tmp_path / "idem.db")
    with engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO projects "
            "(id, name, repo_path, test_command, github_token, github_owner, github_repo) "
            "VALUES ('p1','n','/a','pytest','tok','owner','repo')"
        ))
        conn.commit()

        _migrate_db(conn)
        conn.commit()
        # Operator later switches the backfilled project to local_only.
        conn.execute(text(
            "UPDATE projects SET pr_mode = 'local_only' WHERE id = 'p1'"
        ))
        conn.commit()
        # Re-running migration must not flip it back to manual_token.
        _migrate_db(conn)
        conn.commit()

        mode = conn.execute(
            text("SELECT pr_mode FROM projects WHERE id = 'p1'")
        ).scalar()

    assert mode == "local_only"
