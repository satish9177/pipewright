"""
test_memory_repo_reality.py
Tests for manual DB memory verification against repo reality (M1.5 PR #16C).

No run blocking, no prompt-format change, no schema change. Verifies the manual
service and API endpoint, and that stale-on-conflict facts are excluded from the
prompt block by the existing builder.
"""

import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.memory.memory_store import add_fact, list_facts, mark_fact_stale
from backend.memory.prompt_builder import build_project_memory_block
from backend.memory.repo_reality import (
    evaluate_db_memory_conflicts,
    verify_project_db_memory_against_repo,
)

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def repo():
    root = LOCAL_TMP / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def project_factory(client):
    project_ids: list[str] = []

    def create_project(repo_path: Path, name: str | None = None) -> str:
        response = client.post("/projects", json={
            "name": name or f"RepoReality {uuid.uuid4()}",
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
            conn.execute(text(
                "DELETE FROM memory_suggestions WHERE project_id = :p"
            ), {"p": project_id})
            conn.execute(text(
                "DELETE FROM memory_facts WHERE project_id = :p"
            ), {"p": project_id})
        conn.commit()


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _add_db_fact(project_id: str, content: str) -> str:
    fact = add_fact(
        project_id=project_id,
        content=content,
        category="db",
        scope="backend",
        source="manual",
        added_by="test",
        approved_by="test",
    )
    return fact["id"]


def _fact_by_id(project_id: str, fact_id: str) -> dict:
    return next(f for f in list_facts(project_id) if f["id"] == fact_id)


# --- service: matching -> verified, not stale ------------------------------

def test_matching_db_memory_is_verified_not_staled(repo, project_factory):
    _write(repo, "requirements.txt", "fastapi\npsycopg2-binary\n")
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert result["repo_db_signal"] == "postgresql"
    assert result["ambiguous"] is False
    assert fact_id in result["verified_fact_ids"]
    assert result["staled_fact_ids"] == []

    fact = _fact_by_id(project_id, fact_id)
    assert fact["status"] == "active"
    assert fact["is_stale"] in (0, False)
    assert fact["last_verified_at"] is not None


# --- service: conflict -> staled, content untouched ------------------------

def test_conflicting_db_memory_is_marked_stale(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert result["repo_db_signal"] == "mongodb"
    assert fact_id in result["staled_fact_ids"]
    assert result["verified_fact_ids"] == []

    fact = _fact_by_id(project_id, fact_id)
    assert fact["status"] == "stale"
    assert fact["is_stale"] in (1, True)
    # Content is never edited.
    assert fact["content"] == "Project uses PostgreSQL."
    # Evidence references repo value but no raw file content.
    assert result["evidence"][0]["repo_value"] == "mongodb"
    assert result["evidence"][0]["memory_value"] == "postgresql"


def test_staled_fact_excluded_from_prompt_block(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses PostgreSQL.")

    before = build_project_memory_block(project_id=project_id, role="coder")
    assert "PostgreSQL" in before

    verify_project_db_memory_against_repo(project_id, str(repo))

    after = build_project_memory_block(project_id=project_id, role="coder")
    assert "PostgreSQL" not in after


# --- service: unknown / ambiguous repo signal -> no staling ----------------

def test_unknown_repo_signal_does_not_stale(repo, project_factory):
    _write(repo, "README.md", "# docs only, no manifests")
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert result["repo_db_signal"] is None
    assert result["staled_fact_ids"] == []
    assert result["verified_fact_ids"] == []
    assert result["warnings"]
    assert _fact_by_id(project_id, fact_id)["status"] == "active"


def test_ambiguous_repo_signal_does_not_stale(repo, project_factory):
    _write(repo, "requirements.txt", "psycopg2\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert result["ambiguous"] is True
    assert result["staled_fact_ids"] == []
    assert result["verified_fact_ids"] == []
    assert _fact_by_id(project_id, fact_id)["status"] == "active"


# --- service: no active DB memory; unknown memory value --------------------

def test_no_active_db_memory_is_no_op(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert result["checked_count"] == 0
    assert result["staled_fact_ids"] == []
    assert any("No active DB memory" in w for w in result["warnings"])


def test_memory_with_unknown_db_value_is_skipped_not_staled(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    # "SQLAlchemy" is an ORM, not an engine -> no recognizable DB value.
    fact_id = _add_db_fact(project_id, "Backend uses SQLAlchemy for database access.")

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert fact_id in result["skipped_fact_ids"]
    assert result["staled_fact_ids"] == []
    assert _fact_by_id(project_id, fact_id)["status"] == "active"


def test_memory_mentioning_multiple_engines_is_skipped(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(
        project_id,
        "Migrating from PostgreSQL to MySQL; both run during cutover.",
    )

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert fact_id in result["skipped_fact_ids"]
    assert result["staled_fact_ids"] == []


# --- project isolation ------------------------------------------------------

def test_project_isolation_conflict_does_not_affect_other_project(
    repo, project_factory
):
    # Project A: Mongo repo, Postgres memory -> conflict.
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_a = project_factory(repo, "Reality A")
    fact_a = _add_db_fact(project_a, "Project uses PostgreSQL.")

    # Project B: separate repo + its own Postgres memory.
    repo_b = LOCAL_TMP / str(uuid.uuid4())
    repo_b.mkdir(parents=True, exist_ok=True)
    _write(repo_b, "requirements.txt", "psycopg2-binary\n")
    project_b = project_factory(repo_b, "Reality B")
    fact_b = _add_db_fact(project_b, "Project uses PostgreSQL.")

    try:
        result = verify_project_db_memory_against_repo(project_a, str(repo))
        assert fact_a in result["staled_fact_ids"]
        # Project B is untouched by verifying project A.
        assert _fact_by_id(project_b, fact_b)["status"] == "active"
    finally:
        shutil.rmtree(repo_b, ignore_errors=True)


# --- evidence redaction: .env values never appear --------------------------

def test_evidence_never_contains_env_values(repo, project_factory):
    secret = "sk-thisisaverylongsecretkeyvalue"
    _write(repo, ".env", f"MONGO_URL=mongodb://{secret}@host/db\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses PostgreSQL.")

    result = verify_project_db_memory_against_repo(project_id, str(repo))

    assert secret not in str(result)
    for entry in result["evidence"]:
        assert secret not in str(entry)
        # Evidence excerpt is the fingerprint's fixed human string.
        assert entry["evidence_excerpt"].startswith("Detected ")


# --- API endpoint -----------------------------------------------------------

def test_verify_repo_endpoint_returns_structured_result(repo, project_factory):
    client = TestClient(app)
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")

    response = client.post(f"/api/v1/projects/{project_id}/memory/verify-repo")

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id
    assert body["repo_db_signal"] == "mongodb"
    assert fact_id in body["staled_fact_ids"]
    assert "evidence" in body and body["evidence"][0]["repo_value"] == "mongodb"


def test_verify_repo_endpoint_unknown_project_is_404():
    client = TestClient(app)
    response = client.post("/api/v1/projects/does-not-exist/memory/verify-repo")
    assert response.status_code == 404


# --- read-only evaluator (#16D-3) ------------------------------------------

def test_evaluator_is_read_only(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")

    before = _fact_by_id(project_id, fact_id)
    report = evaluate_db_memory_conflicts(project_id, str(repo))
    after = _fact_by_id(project_id, fact_id)

    # Conflict detected...
    assert report.repo_db_signal == "mongodb"
    assert any(entry.fact_id == fact_id for entry in report.conflicts)
    # ...but nothing mutated: status, staleness, and verification stamp unchanged.
    assert after["status"] == before["status"] == "active"
    assert after["is_stale"] == before["is_stale"]
    assert after["last_verified_at"] == before["last_verified_at"]


def test_evaluator_detects_conflict_among_stale_facts(repo, project_factory):
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")
    # Pre-stale the fact (as #16C's manual action would). Default eval statuses
    # include 'stale', so the conflict must still surface.
    mark_fact_stale(project_id, fact_id, reason="prior conflict")
    assert _fact_by_id(project_id, fact_id)["status"] == "stale"

    report = evaluate_db_memory_conflicts(project_id, str(repo))

    conflict = next(e for e in report.conflicts if e.fact_id == fact_id)
    assert conflict.status == "stale"
    assert conflict.repo_value == "mongodb"


def test_evaluator_no_conflict_when_memory_matches(repo, project_factory):
    _write(repo, "requirements.txt", "pymongo\n")
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses MongoDB.")

    report = evaluate_db_memory_conflicts(project_id, str(repo))

    assert report.conflicts == ()
    assert fact_id in report.matches
