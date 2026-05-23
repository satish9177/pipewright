"""
test_projects.py
Tests for persisted project configuration.
No API calls. No Gemini.
"""

import uuid
import shutil
from pathlib import Path

import pytest

from backend.projects.project_store import (
    create_project,
    get_project,
    list_projects,
)

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def tmp_repo():
    folder = LOCAL_TMP / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def test_create_project_returns_project_record(tmp_repo):
    project = create_project(
        name=f"Test Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )

    assert project["id"].startswith("proj-")
    assert project["name"].startswith("Test Project")
    assert project["repo_path"] == str(tmp_repo)
    assert project["test_command"] == "python --version"


def test_get_project_returns_saved_project(tmp_repo):
    created = create_project(
        name=f"Lookup Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )

    loaded = get_project(created["id"])

    assert loaded is not None
    assert loaded["id"] == created["id"]
    assert loaded["name"] == created["name"]


def test_list_projects_returns_list():
    projects = list_projects()
    assert isinstance(projects, list)


def test_create_project_requires_name(tmp_repo):
    with pytest.raises(ValueError):
        create_project("", str(tmp_repo), "python --version")


def test_create_project_stores_branch_and_description(tmp_repo):
    project = create_project(
        name=f"Branch Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
        branch="develop",
        description="Test description",
    )

    assert project["branch"] == "develop"
    assert project["description"] == "Test description"


def test_create_project_defaults_github_base_branch(tmp_repo):
    project = create_project(
        name=f"Default Base {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )

    assert project["github_base_branch"] == "pipewright-staging"


def test_create_project_stores_github_fields(tmp_repo):
    project = create_project(
        name=f"GitHub Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
        github_token="secret-token",
        github_owner="owner",
        github_repo="repo",
        github_base_branch="pipewright-staging",
    )

    assert project["github_token"] == "secret-token"
    assert project["github_owner"] == "owner"
    assert project["github_repo"] == "repo"
    assert project["github_base_branch"] == "pipewright-staging"


def test_create_project_requires_test_command(tmp_repo):
    with pytest.raises(ValueError):
        create_project(f"Missing Command {uuid.uuid4()}", str(tmp_repo), "")
