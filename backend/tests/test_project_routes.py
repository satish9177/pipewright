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


def test_run_requires_existing_project():
    client = TestClient(app)

    response = client.post("/run", json={
        "project_id": "proj-missing",
        "feature_description": "Add ping endpoint",
    })

    assert response.status_code == 404
