"""
test_project_detect_test_command.py
Wiring tests for detect-and-prefill of a test command on POST /projects/detect
(Phase 0, G4). No API calls. No Gemini. No subprocess execution of the command.

The detect endpoint is read-only: it suggests a command from repo markers for
the New Project form and persists nothing, so it can never overwrite a
configured command. These tests prove detected / not-detected / no-overwrite.
"""

import shutil
import uuid
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


def test_detect_suggests_pytest_when_marker_present(tmp_repo):
    (tmp_repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    client = TestClient(app)

    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})

    assert response.status_code == 200
    assert response.json()["suggested_test_command"] == "pytest"


def test_detect_suggests_npm_test_for_jest(tmp_repo):
    (tmp_repo / "package.json").write_text(
        '{"devDependencies": {"jest": "^29.0.0"}}', encoding="utf-8"
    )
    client = TestClient(app)

    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})

    assert response.status_code == 200
    assert response.json()["suggested_test_command"] == "npm test"


def test_detect_returns_null_when_no_signal(tmp_repo):
    # Empty repo: no markers. Detection must not fail setup; it returns null.
    client = TestClient(app)

    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})

    assert response.status_code == 200
    assert response.json()["suggested_test_command"] is None


def test_detect_does_not_overwrite_configured_command(tmp_repo):
    """Detection is read-only: a configured project's command is untouched."""
    client = TestClient(app)
    created = client.post("/projects", json={
        "name": "Configured Command Project",
        "repo_path": str(tmp_repo),
        "test_command": "make test",
    }).json()

    # A pytest marker would *suggest* "pytest", but detect persists nothing.
    (tmp_repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    detect = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})
    assert detect.status_code == 200
    assert detect.json()["suggested_test_command"] == "pytest"

    # The stored command is unchanged — detection never wrote it.
    fetched = client.get(f"/projects/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["test_command"] == "make test"
