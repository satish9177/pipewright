"""
test_project_routes.py
Tests for project API routes.
No API calls. No Gemini.
"""

import uuid
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.projects.project_store import get_project

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def tmp_repo():
    folder = LOCAL_TMP / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def test_create_project_route(tmp_repo):
    client = TestClient(app)

    response = client.post("/projects", json={
        "name": "AI Workflow Platform",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["id"].startswith("proj-")
    assert data["name"] == "AI Workflow Platform"
    assert data["repo_path"] == str(tmp_repo)
    assert "github_token" not in data
    assert data["has_github_token"] is False


def test_create_project_with_github_token_sanitizes_response(tmp_repo):
    client = TestClient(app)

    response = client.post("/projects", json={
        "name": "Token Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
        "github_token": "secret-token",
        "github_owner": "owner",
        "github_repo": "repo",
    })

    assert response.status_code == 200
    data = response.json()
    assert "github_token" not in data
    assert data["has_github_token"] is True

    stored_project = get_project(data["id"])
    assert stored_project["github_token"] == "secret-token"


def test_get_project_sanitizes_github_token(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Get Token Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
        "github_token": "secret-token",
    })
    project_id = create_response.json()["id"]

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    data = response.json()
    assert "github_token" not in data
    assert data["has_github_token"] is True


def test_get_project_without_token_returns_false_flag(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "No Token Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.get(f"/projects/{project_id}")

    assert response.status_code == 200
    data = response.json()
    assert "github_token" not in data
    assert data["has_github_token"] is False


def test_list_projects_sanitizes_all_github_tokens(tmp_repo):
    client = TestClient(app)
    client.post("/projects", json={
        "name": "List Token Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
        "github_token": "secret-token",
    })

    response = client.get("/projects")

    assert response.status_code == 200
    projects = response.json()
    assert isinstance(projects, list)
    assert all("github_token" not in project for project in projects)
    assert any(project["has_github_token"] is True for project in projects)


def test_update_project_sanitizes_github_token(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Token Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "github_token": "updated-secret-token",
        "github_owner": "owner",
        "github_repo": "repo",
    })

    assert response.status_code == 200
    data = response.json()
    assert "github_token" not in data
    assert data["has_github_token"] is True


def test_run_requires_existing_project():
    client = TestClient(app)

    response = client.post("/run", json={
        "project_id": "proj-missing",
        "feature_description": "Add ping endpoint",
    })

    assert response.status_code == 404
