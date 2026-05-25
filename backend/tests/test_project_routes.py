"""
test_project_routes.py
Tests for project API routes.
No API calls. No Gemini.
"""

import uuid
import shutil
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.main import app
from backend.projects.project_store import get_project
from backend.security.secrets import decrypt_secret

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


def test_create_project_with_github_token_sanitizes_response(tmp_repo, monkeypatch):
    monkeypatch.setenv("PIPEWRIGHT_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
    assert "secret-token" not in response.text

    stored_project = get_project(data["id"])
    assert stored_project["github_token"] != "secret-token"
    assert decrypt_secret(stored_project["github_token"]) == "secret-token"


def test_get_project_sanitizes_github_token(tmp_repo, monkeypatch):
    monkeypatch.setenv("PIPEWRIGHT_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
    assert "secret-token" not in response.text


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


def test_list_projects_sanitizes_all_github_tokens(tmp_repo, monkeypatch):
    monkeypatch.setenv("PIPEWRIGHT_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
    assert "secret-token" not in response.text
    assert any(project["has_github_token"] is True for project in projects)


def test_update_project_sanitizes_github_token(tmp_repo, monkeypatch):
    monkeypatch.setenv("PIPEWRIGHT_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
    assert "updated-secret-token" not in response.text


def test_project_create_rejects_empty_name(tmp_repo):
    client = TestClient(app)

    response = client.post("/projects", json={
        "name": "",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })

    assert response.status_code == 422


def test_project_create_rejects_too_long_name(tmp_repo):
    client = TestClient(app)

    response = client.post("/projects", json={
        "name": "x" * 121,
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })

    assert response.status_code == 422


def test_project_create_rejects_too_long_repo_path():
    client = TestClient(app)

    response = client.post("/projects", json={
        "name": "Long Repo Path",
        "repo_path": "x" * 1001,
        "test_command": "python --version",
    })

    assert response.status_code == 422


def test_project_create_rejects_too_long_test_command(tmp_repo):
    client = TestClient(app)

    response = client.post("/projects", json={
        "name": "Long Test Command",
        "repo_path": str(tmp_repo),
        "test_command": "x" * 1001,
    })

    assert response.status_code == 422


def test_project_update_rejects_empty_name_when_provided(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Validation Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "name": "",
    })

    assert response.status_code == 422


def test_project_update_rejects_too_long_name_when_provided(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Validation Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "name": "x" * 121,
    })

    assert response.status_code == 422


def test_project_update_allows_omitted_fields(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Omitted Fields",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={})

    assert response.status_code == 200
    assert response.json()["id"] == project_id


def test_project_update_allows_null_description(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Null Description",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
        "description": "keep me",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "description": None,
    })

    assert response.status_code == 200
    assert response.json()["description"] == "keep me"


def test_project_update_allows_blank_description(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Blank Description",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
        "description": "clear me",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "description": "",
    })

    assert response.status_code == 200
    assert response.json()["description"] == ""


def test_project_update_rejects_too_long_description(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Long Description",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "description": "x" * 2001,
    })

    assert response.status_code == 422


def test_project_update_allows_null_branch(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Null Branch",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
        "branch": "develop",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "branch": None,
    })

    assert response.status_code == 200
    assert response.json()["branch"] == "develop"


def test_project_update_rejects_blank_branch_when_provided(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Patch Blank Branch",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.patch(f"/projects/{project_id}", json={
        "branch": "",
    })

    assert response.status_code == 422


def test_run_rejects_empty_feature_description(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Run Validation Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.post("/run", json={
        "project_id": project_id,
        "feature_description": "",
    })

    assert response.status_code == 422


def test_run_rejects_too_long_feature_description(tmp_repo):
    client = TestClient(app)
    create_response = client.post("/projects", json={
        "name": "Run Validation Project",
        "repo_path": str(tmp_repo),
        "test_command": "python --version",
    })
    project_id = create_response.json()["id"]

    response = client.post("/run", json={
        "project_id": project_id,
        "feature_description": "x" * 12001,
    })

    assert response.status_code == 422


def test_gate_reject_rejects_too_long_reason():
    client = TestClient(app)

    response = client.post("/gates/gate-missing/reject", json={
        "reason": "x" * 2001,
    })

    assert response.status_code == 422


def test_run_requires_existing_project():
    client = TestClient(app)

    response = client.post("/run", json={
        "project_id": "proj-missing",
        "feature_description": "Add ping endpoint",
    })

    assert response.status_code == 404
