"""
Parity tests for bootstrap detection rules.

Golden provenance:
  The synthetic snapshots in goldens/memory_detection_rules.json were generated
  from the pre-refactor backend.memory.bootstrap._collect_candidates behavior.
  To reproduce the baseline, check out the pre-refactor base, apply this test
  file/fixture matrix without backend/memory/detection_rules.py, then run with
  PIPEWRIGHT_UPDATE_DETECTION_RULE_GOLDENS=1. The update path refuses to run on
  the refactored branch so goldens are not regenerated from the new evaluator.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.memory.bootstrap import (
    CandidateSuggestion,
    _collect_candidates,
    generate_bootstrap_suggestions,
)
from backend.memory.detection_rules import collect_detection_candidates

pytestmark = pytest.mark.unit

FIELDS = (
    "content",
    "category",
    "scope",
    "priority",
    "evidence_path",
    "evidence_excerpt",
)
GOLDEN_PATH = Path(__file__).parent / "goldens" / "memory_detection_rules.json"
FOLLOWUP_GOLDEN_PATH = (
    Path(__file__).parent / "goldens" / "memory_detection_rules_followups.json"
)
LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"
REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_GOLDENS_ENV = "PIPEWRIGHT_UPDATE_DETECTION_RULE_GOLDENS"


def _write(root: Path, rel: str, content: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _candidate_dict(candidate: CandidateSuggestion) -> dict:
    data = asdict(candidate)
    return {field: data[field] for field in FIELDS}


def _snapshot(root: Path) -> list[dict]:
    return [_candidate_dict(candidate) for candidate in _collect_candidates(root)]


def _serialize(payload: dict[str, list[dict]]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _empty_repo(_root: Path) -> None:
    pass


def _python_manifest_order(root: Path) -> None:
    _write(root, "pyproject.toml", "[project]\ndependencies = ['flask', 'pytest']\n")
    _write(
        root,
        "requirements.txt",
        "fastapi\nuvicorn\nsqlalchemy\npsycopg2\nmysqlclient\nmongoose\n",
    )


def _django_manifest_rule(root: Path) -> None:
    _write(root, "requirements.txt", "django\n")


def _python_311_first_evidence(root: Path) -> None:
    _write(root, "pyproject.toml", "[project]\nrequires-python = '>=3.11'\n")
    _write(root, "requirements.txt", "python_version == '3.11'\n")


def _python_311_caret_pattern(root: Path) -> None:
    _write(root, "pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.11"\n')


def _python_311_docker_pattern(root: Path) -> None:
    _write(root, "Dockerfile", "FROM python:3.11-slim\n")


def _package_json_order_and_variants(root: Path) -> None:
    _write(
        root,
        "package.json",
        json.dumps({
            "scripts": {"build": "vite build", "test": "vitest run"},
            "dependencies": {"react": "latest"},
            "devDependencies": {
                "vite": "latest",
                "typescript": "latest",
                "jest": "latest",
                "vitest": "latest",
            },
        }, indent=2),
    )
    _write(
        root,
        "services/api/package.json",
        json.dumps({
            "scripts": {"test": "node test.js"},
            "dependencies": {
                "express": "latest",
                "@nestjs/core": "latest",
                "fastify": "latest",
                "prisma": "latest",
                "@prisma/client": "latest",
                "mongoose": "latest",
            },
        }, indent=2),
    )


def _next_peer_dependency(root: Path) -> None:
    _write(
        root,
        "package.json",
        json.dumps({"peerDependencies": {"next": "latest"}}, indent=2),
    )


def _malformed_package_json(root: Path) -> None:
    _write(root, "package.json", "react vite nestjs mongodb @nestjs/core")


def _jvm_go_rust_order(root: Path) -> None:
    _write(
        root,
        "pom.xml",
        "<project><dependency>spring-boot</dependency>"
        "<dependency>junit</dependency></project>",
    )
    _write(root, "backend/pom.xml", "<project>maven spring-boot junit</project>")
    _write(
        root,
        "build.gradle",
        "plugins { id 'java' }\n"
        "dependencies { testImplementation 'junit:junit:4.13' }\n",
    )
    _write(root, "go.mod", "module example.com/demo\n")
    _write(root, "frontend/Cargo.toml", "[package]\nname = 'web-crate'\n")


def _first_evidence_filesystem(root: Path) -> None:
    _write(root, "schema.sql", "CREATE TABLE example (id INTEGER PRIMARY KEY);\n")
    _write(root, "docker-compose.yml", "services:\n  app:\n    image: example/app\n")
    _write(root, "docker-compose.yaml", "services:\n  db:\n    image: postgres:16\n")
    _write(root, "prisma/schema.prisma", "datasource db { provider = 'sqlite' }\n")
    (root / "alembic").mkdir(parents=True, exist_ok=True)
    _write(root, "backend/pipeline/patch_applier.py", "# patch applier marker\n")


def _bare_dockerfile(root: Path) -> None:
    _write(root, "Dockerfile", "FROM alpine:3.20\n")


def _alembic_ini_file_branch(root: Path) -> None:
    _write(root, "alembic.ini", "[alembic]\nscript_location = alembic\n")


def _prisma_package_and_schema(root: Path) -> None:
    _write(
        root,
        "frontend/package.json",
        json.dumps({"dependencies": {"prisma": "latest"}}, indent=2),
    )
    _write(root, "prisma/schema.prisma", "datasource db { provider = 'postgresql' }\n")


def _scope_and_test_script_branches(root: Path) -> None:
    _write(
        root,
        "frontend/package.json",
        json.dumps({
            "scripts": {"test": "vitest run"},
            "dependencies": {"react": "latest"},
            "devDependencies": {"vite": "latest"},
        }, indent=2),
    )
    _write(
        root,
        "server/package.json",
        json.dumps({"scripts": {"test": "node test.js"}}, indent=2),
    )
    _write(root, "tests/Cargo.toml", "[package]\nname = 'test-crate'\n")
    _write(root, "infra/Cargo.toml", "[package]\nname = 'infra-crate'\n")


FIXTURE_BUILDERS = {
    "empty_repo": _empty_repo,
    "python_manifest_order": _python_manifest_order,
    "python_311_first_evidence": _python_311_first_evidence,
    "package_json_order_and_variants": _package_json_order_and_variants,
    "malformed_package_json": _malformed_package_json,
    "jvm_go_rust_order": _jvm_go_rust_order,
    "first_evidence_filesystem": _first_evidence_filesystem,
    "prisma_package_and_schema": _prisma_package_and_schema,
    "scope_and_test_script_branches": _scope_and_test_script_branches,
}

FOLLOWUP_FIXTURE_BUILDERS = {
    "alembic_ini_file_branch": _alembic_ini_file_branch,
    "bare_dockerfile": _bare_dockerfile,
    "django_manifest_rule": _django_manifest_rule,
    "next_peer_dependency": _next_peer_dependency,
    "python_311_caret_pattern": _python_311_caret_pattern,
    "python_311_docker_pattern": _python_311_docker_pattern,
}


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
            "name": name or f"Detection Rules Project {uuid.uuid4()}",
            "repo_path": str(repo_path),
            "test_command": "python --version",
        })
        assert response.status_code == 200
        project_id = response.json()["id"]
        project_ids.append(project_id)
        return project_id

    yield create_project

    with engine.begin() as conn:
        for project_id in project_ids:
            conn.execute(text("""
                DELETE FROM memory_suggestions WHERE project_id = :project_id
            """), {"project_id": project_id})
            conn.execute(text("""
                DELETE FROM memory_facts WHERE project_id = :project_id
            """), {"project_id": project_id})
            conn.execute(text("""
                DELETE FROM projects WHERE id = :project_id
            """), {"project_id": project_id})


def test_collect_candidates_matches_pre_refactor_goldens(tmp_path):
    actual: dict[str, list[dict]] = {}
    for name, builder in FIXTURE_BUILDERS.items():
        root = tmp_path / name
        root.mkdir()
        builder(root)
        actual[name] = _snapshot(root)

    if os.environ.get(UPDATE_GOLDENS_ENV):
        if (REPO_ROOT / "backend" / "memory" / "detection_rules.py").exists():
            pytest.fail("refusing to regenerate detection goldens from refactored code")
        GOLDEN_PATH.write_text(_serialize(actual), encoding="utf-8")

    assert _serialize(actual) == GOLDEN_PATH.read_text(encoding="utf-8")


def test_followup_fixture_coverage_matches_goldens(tmp_path):
    actual: dict[str, list[dict]] = {}
    for name, builder in FOLLOWUP_FIXTURE_BUILDERS.items():
        root = tmp_path / name
        root.mkdir()
        builder(root)
        actual[name] = _snapshot(root)

    assert _serialize(actual) == FOLLOWUP_GOLDEN_PATH.read_text(encoding="utf-8")


def test_evaluator_is_pure_and_order_stable(tmp_path):
    files = {
        "pyproject.toml": "[project]\ndependencies = ['flask']\n",
        "requirements.txt": "fastapi\npytest\n",
        "package.json": json.dumps({"scripts": {"test": "node test.js"}}),
    }
    lowered = {path: content.lower() for path, content in files.items()}
    files_before = dict(files)
    lowered_before = dict(lowered)

    first = collect_detection_candidates(root=tmp_path, files=files, lowered=lowered)
    second = collect_detection_candidates(root=tmp_path, files=files, lowered=lowered)

    assert first == second
    assert files == files_before
    assert lowered == lowered_before
    assert [candidate.content for candidate in first[:4]] == [
        "Backend uses Flask.",
        "Backend uses FastAPI.",
        "Run backend unit tests with pytest.",
        "Project defines npm test script.",
    ]


def test_empty_evaluator_input_emits_only_bootstrap_default(tmp_path):
    candidates = collect_detection_candidates(root=tmp_path, files={}, lowered={})

    assert [_candidate_dict(candidate) for candidate in candidates] == [
        {
            "content": "Never log secrets, API keys, tokens, or .env values.",
            "category": "security",
            "scope": "global",
            "priority": 0,
            "evidence_path": "__bootstrap_default__",
            "evidence_excerpt": "Default bootstrap safety rule.",
        }
    ]


def test_generate_bootstrap_suggestions_keeps_mongodb_first_seen_evidence(
    project_factory,
    project_repo,
):
    _write(project_repo, "requirements.txt", "mongoose\n")
    _write(project_repo, "package.json", '{"dependencies": {"mongoose": "latest"}}')
    project_id = project_factory(project_repo)

    suggestions = generate_bootstrap_suggestions(project_id)

    matches = [
        suggestion for suggestion in suggestions
        if suggestion["content"] == "Project uses MongoDB."
    ]
    assert len(matches) == 1
    assert matches[0]["evidence_path"] == "requirements.txt"
    assert matches[0]["evidence_excerpt"] == (
        "Detected MongoDB dependency or reference."
    )


def test_generate_bootstrap_suggestions_keeps_pytest_first_seen_evidence(
    project_factory,
    project_repo,
):
    _write(project_repo, "requirements.txt", "pytest\n")
    _write(project_repo, "pytest.ini", "[pytest]\nmarkers = unit\n")
    project_id = project_factory(project_repo)

    suggestions = generate_bootstrap_suggestions(project_id)

    matches = [
        suggestion for suggestion in suggestions
        if suggestion["content"] == "Run backend unit tests with pytest."
    ]
    assert len(matches) == 1
    assert matches[0]["evidence_path"] == "requirements.txt"
    assert matches[0]["evidence_excerpt"] == "Detected pytest dependency."
