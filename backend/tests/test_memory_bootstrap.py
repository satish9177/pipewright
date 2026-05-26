"""
test_memory_bootstrap.py
Tests for deterministic bootstrap memory suggestions.
"""

import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.memory import bootstrap
from backend.memory.bootstrap import CandidateSuggestion, generate_bootstrap_suggestions
from backend.memory.memory_store import add_fact

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_repo():
    root = LOCAL_TMP / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def project_factory(client):
    project_ids: list[str] = []

    def create_project(repo_path: Path, name: str | None = None) -> str:
        response = client.post("/projects", json={
            "name": name or f"Bootstrap Project {uuid.uuid4()}",
            "repo_path": str(repo_path),
            "test_command": "python --version",
        })
        assert response.status_code == 200
        project_id = response.json()["id"]
        project_ids.append(project_id)
        return project_id

    yield create_project

    with engine.connect() as conn:
        for project_id in project_ids:
            conn.execute(text("""
                DELETE FROM memory_suggestions WHERE project_id = :project_id
            """), {"project_id": project_id})
            conn.execute(text("""
                DELETE FROM memory_facts WHERE project_id = :project_id
            """), {"project_id": project_id})
        conn.commit()


def _write_basic_python_repo(root: Path) -> None:
    (root / "requirements.txt").write_text(
        "fastapi\nsqlalchemy\npytest\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text("[pytest]\nmarkers = unit\n", encoding="utf-8")
    db_dir = root / "backend" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "schema.sql").write_text(
        "CREATE TABLE example (id TEXT PRIMARY KEY);\n",
        encoding="utf-8",
    )


def _write_frontend_repo(root: Path) -> None:
    (root / "package.json").write_text(
        """
        {
          "scripts": {"build": "vite build"},
          "dependencies": {"react": "latest"},
          "devDependencies": {"vite": "latest", "typescript": "latest"}
        }
        """,
        encoding="utf-8",
    )


def _write_json(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _bootstrap(client, project_id: str, force: bool = False):
    return client.post(
        f"/api/v1/projects/{project_id}/memory/bootstrap-suggestions",
        json={"force": force},
    )


def _list_suggestions(client, project_id: str, status: str | None = None):
    params = {"status": status} if status else None
    return client.get(
        f"/api/v1/projects/{project_id}/memory/suggestions",
        params=params,
    )


def test_generate_bootstrap_suggestions_creates_pending_suggestions(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert suggestions
    assert all(suggestion["status"] == "pending" for suggestion in suggestions)
    assert "content_hash" not in suggestions[0]
    assert any(
        suggestion["content"] == "Backend uses FastAPI."
        for suggestion in suggestions
    )


def test_bootstrap_suggestions_are_project_scoped(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_a = project_factory(project_repo, "Bootstrap A")
    project_b_repo = LOCAL_TMP / str(uuid.uuid4())
    project_b_repo.mkdir(parents=True, exist_ok=True)
    project_b = project_factory(project_b_repo, "Bootstrap B")
    try:
        _bootstrap(client, project_a)

        response_a = _list_suggestions(client, project_a)
        response_b = _list_suggestions(client, project_b)

        assert response_a.status_code == 200
        assert response_b.status_code == 200
        assert response_a.json()["suggestions"]
        assert response_b.json()["suggestions"] == []
    finally:
        shutil.rmtree(project_b_repo, ignore_errors=True)


def test_bootstrap_skips_active_duplicate_memory(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    add_fact(
        project_id=project_id,
        content="Backend uses FastAPI.",
        category="stack",
        scope="backend",
        source="manual",
        added_by="test",
        approved_by="test",
    )

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert all(
        suggestion["content"] != "Backend uses FastAPI."
        for suggestion in response.json()["suggestions"]
    )


def test_bootstrap_skips_pending_duplicate_suggestion(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    first = _bootstrap(client, project_id)
    second = _bootstrap(client, project_id)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["suggestions"]
    assert second.json()["suggestions"] == []


def test_bootstrap_force_does_not_create_active_duplicate(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    add_fact(
        project_id=project_id,
        content="Backend uses FastAPI.",
        category="stack",
        scope="backend",
        source="manual",
        added_by="test",
        approved_by="test",
    )

    response = _bootstrap(client, project_id, force=True)

    assert response.status_code == 200
    assert all(
        suggestion["content"] != "Backend uses FastAPI."
        for suggestion in response.json()["suggestions"]
    )


def test_list_suggestions_api(client, project_factory, project_repo):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    _bootstrap(client, project_id)

    response = _list_suggestions(client, project_id, status="pending")

    assert response.status_code == 200
    assert response.json()["project_id"] == project_id
    assert response.json()["suggestions"]
    assert all(
        suggestion["status"] == "pending"
        for suggestion in response.json()["suggestions"]
    )


def test_approve_suggestion_creates_active_memory_fact(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    suggestion = _bootstrap(client, project_id).json()["suggestions"][0]

    response = client.post(
        f"/api/v1/projects/{project_id}/memory/suggestions/{suggestion['id']}/approve",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["suggestion"]["status"] == "approved"
    assert data["fact"]["content"] == suggestion["content"]

    memory_response = client.get(
        f"/api/v1/projects/{project_id}/memory",
        params={"status": "active"},
    )
    preview_response = client.get(
        f"/api/v1/projects/{project_id}/memory/prompt-preview",
        params={"role": "coder"},
    )
    assert suggestion["content"] in memory_response.text
    assert "PROJECT MEMORY" in preview_response.json()["memory_block"]


def test_approve_suggestion_not_cross_project(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_a = project_factory(project_repo, "Approve A")
    project_b_repo = LOCAL_TMP / str(uuid.uuid4())
    project_b_repo.mkdir(parents=True, exist_ok=True)
    project_b = project_factory(project_b_repo, "Approve B")
    try:
        suggestion = _bootstrap(client, project_a).json()["suggestions"][0]

        response = client.post(
            f"/api/v1/projects/{project_b}/memory/suggestions/{suggestion['id']}/approve",
        )

        assert response.status_code == 404
    finally:
        shutil.rmtree(project_b_repo, ignore_errors=True)


def test_reject_suggestion_does_not_create_memory(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    suggestion = _bootstrap(client, project_id).json()["suggestions"][0]

    response = client.post(
        f"/api/v1/projects/{project_id}/memory/suggestions/{suggestion['id']}/reject",
        json={"reason": "Not accurate for this project."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    memory_response = client.get(
        f"/api/v1/projects/{project_id}/memory",
        params={"status": "active"},
    )
    assert suggestion["content"] not in memory_response.text


def test_reject_requires_reason(client, project_factory, project_repo):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    suggestion = _bootstrap(client, project_id).json()["suggestions"][0]

    response = client.post(
        f"/api/v1/projects/{project_id}/memory/suggestions/{suggestion['id']}/reject",
        json={"reason": ""},
    )

    assert response.status_code == 422


def test_approved_suggestion_cannot_be_approved_twice(
    client,
    project_factory,
    project_repo,
):
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)
    suggestion = _bootstrap(client, project_id).json()["suggestions"][0]
    approve_path = (
        f"/api/v1/projects/{project_id}/memory/suggestions/"
        f"{suggestion['id']}/approve"
    )

    first = client.post(approve_path)
    second = client.post(approve_path)

    assert first.status_code == 200
    assert second.status_code == 409


def test_bootstrap_does_not_read_dotenv_values(
    client,
    project_factory,
    project_repo,
):
    secret = "sk-thisisaverylongsecretkeyvalue"
    (project_repo / ".env").write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    (project_repo / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert secret not in response.text


def test_bootstrap_validation_rejects_secret_like_suggestion(
    monkeypatch,
    project_factory,
    project_repo,
):
    project_id = project_factory(project_repo)
    monkeypatch.setattr(
        bootstrap,
        "_collect_candidates",
        lambda _root: [
            CandidateSuggestion(
                content="Never store sk-thisisaverylongsecretkeyvalue",
                category="security",
                scope="global",
                evidence_path="requirements.txt",
                evidence_excerpt="Unsafe candidate",
            )
        ],
    )

    suggestions = generate_bootstrap_suggestions(project_id)

    assert suggestions == []


def test_bootstrap_detects_frontend_stack(client, project_factory, project_repo):
    _write_frontend_repo(project_repo)
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    contents = {suggestion["content"] for suggestion in response.json()["suggestions"]}
    assert "Frontend uses React, Vite, and TypeScript." in contents
    assert "Frontend build uses npm run build." in contents


def test_bootstrap_detects_backend_requirements_in_backend_folder(
    client,
    project_factory,
    project_repo,
):
    backend_dir = project_repo / "backend"
    backend_dir.mkdir()
    (backend_dir / "requirements.txt").write_text(
        "fastapi\nuvicorn\nsqlalchemy\npytest\n",
        encoding="utf-8",
    )
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    contents = {suggestion["content"] for suggestion in suggestions}
    assert "Backend uses FastAPI." in contents
    assert "Backend uses SQLAlchemy for database access." in contents
    assert "Run backend unit tests with pytest." in contents
    assert any(
        suggestion["evidence_path"] == "backend/requirements.txt"
        for suggestion in suggestions
        if suggestion["content"] == "Backend uses FastAPI."
    )


def test_bootstrap_detects_backend_from_arbitrary_folder_name(
    client,
    project_factory,
    project_repo,
):
    service_dir = project_repo / "service-main"
    service_dir.mkdir()
    (service_dir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert any(
        suggestion["content"] == "Backend uses FastAPI."
        and suggestion["scope"] == "backend"
        and suggestion["evidence_path"] == "service-main/requirements.txt"
        for suggestion in response.json()["suggestions"]
    )


def test_bootstrap_detects_frontend_from_nested_package_json(
    client,
    project_factory,
    project_repo,
):
    _write_json(
        project_repo / "apps" / "web" / "package.json",
        """
        {
          "scripts": {"build": "vite build"},
          "dependencies": {"react": "latest"},
          "devDependencies": {"vite": "latest", "typescript": "latest"}
        }
        """,
    )
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert any(
        suggestion["content"] == "Frontend uses React, Vite, and TypeScript."
        and suggestion["scope"] == "frontend"
        and suggestion["evidence_path"] == "apps/web/package.json"
        for suggestion in response.json()["suggestions"]
    )


def test_bootstrap_detects_node_backend_from_nested_package_json(
    client,
    project_factory,
    project_repo,
):
    _write_json(
        project_repo / "services" / "payroll" / "package.json",
        """
        {
          "dependencies": {"express": "^4.18.0"}
        }
        """,
    )
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert any(
        suggestion["content"] == "Backend uses Express."
        and suggestion["scope"] == "backend"
        and suggestion["evidence_path"] == "services/payroll/package.json"
        for suggestion in response.json()["suggestions"]
    )


def test_folder_name_does_not_override_dependency_content(
    client,
    project_factory,
    project_repo,
):
    _write_json(
        project_repo / "backend" / "package.json",
        """
        {
          "scripts": {"build": "vite build"},
          "dependencies": {"react": "latest"},
          "devDependencies": {"vite": "latest", "typescript": "latest"}
        }
        """,
    )
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    contents = {suggestion["content"] for suggestion in response.json()["suggestions"]}
    assert "Backend uses Express." not in contents
    assert "Backend uses NestJS." not in contents
    assert "Backend uses Fastify." not in contents
    assert "Frontend uses React, Vite, and TypeScript." in contents


def test_bootstrap_ignores_node_modules_and_dist(
    client,
    project_factory,
    project_repo,
):
    _write_json(
        project_repo / "node_modules" / "old-service" / "package.json",
        '{"dependencies": {"express": "latest"}}',
    )
    _write_json(
        project_repo / "dist" / "package.json",
        '{"dependencies": {"@nestjs/core": "latest"}}',
    )
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    contents = {suggestion["content"] for suggestion in response.json()["suggestions"]}
    assert "Backend uses Express." not in contents
    assert "Backend uses NestJS." not in contents


def test_bootstrap_ignores_examples_templates(
    client,
    project_factory,
    project_repo,
):
    _write_json(
        project_repo / "examples" / "legacy" / "package.json",
        '{"dependencies": {"express": "latest"}}',
    )
    _write_json(
        project_repo / "templates" / "api" / "requirements.txt",
        "fastapi\n",
    )
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    contents = {suggestion["content"] for suggestion in response.json()["suggestions"]}
    assert "Backend uses Express." not in contents
    assert "Backend uses FastAPI." not in contents


def test_bootstrap_respects_max_manifest_files(
    client,
    project_factory,
    project_repo,
    monkeypatch,
):
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_MAX_MANIFEST_FILES", 3)
    for index in range(10):
        folder = project_repo / f"service-{index}"
        folder.mkdir()
        (folder / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    evidence_paths = {
        suggestion["evidence_path"]
        for suggestion in response.json()["suggestions"]
        if suggestion["evidence_path"] and suggestion["evidence_path"].endswith("requirements.txt")
    }
    assert len(evidence_paths) <= 3


def test_bootstrap_reads_env_example_safely_if_supported(
    client,
    project_factory,
    project_repo,
):
    secret = "sk-thisisaverylongsecretkeyvalue"
    (project_repo / ".env.example").write_text(
        f"OPENAI_API_KEY={secret}\n",
        encoding="utf-8",
    )
    _write_basic_python_repo(project_repo)
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert secret not in response.text


def test_bootstrap_evidence_path_is_nested_path(
    client,
    project_factory,
    project_repo,
):
    api_dir = project_repo / "apps" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    project_id = project_factory(project_repo)

    response = _bootstrap(client, project_id)

    assert response.status_code == 200
    assert any(
        suggestion["content"] == "Backend uses FastAPI."
        and suggestion["evidence_path"] == "apps/api/requirements.txt"
        for suggestion in response.json()["suggestions"]
    )
