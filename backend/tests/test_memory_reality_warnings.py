"""
test_memory_reality_warnings.py
Tests for M3F3: read-only repo-reality warning surfacing on the memory injection
analysis path.

Two layers:
  - Pure analysis helper (analyze_injection_events with a caller-supplied
    repo_signals map): mismatch -> advisory warning; match/unknown/unsupported/
    no-signal -> no scary warning; same fact across roles -> single warning.
  - The /analysis endpoint with a REAL project + repo: the repo signal is
    extracted on the read path (capped fingerprint), an unambiguous mismatch is
    surfaced advisory-only, ambiguous/unknown repo signals never warn, and the
    endpoint NEVER mutates memory (contrast with /memory/verify-repo).
"""

import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.main import app
from backend.memory.injection_analysis import analyze_injection_events
from backend.memory.injection_store import record_memory_injection_event
from backend.memory.memory_store import add_fact, list_facts

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


# --- pure-helper fixtures/helpers -------------------------------------------

def _entry(fact_id, content, *, category="db", scope="backend"):
    return {
        "fact_id": fact_id,
        "content": content,
        "content_hash": f"h-{fact_id}",
        "category": category,
        "scope": scope,
        "priority": 100,
        "status_at_injection": "active",
    }


def _event(event_id, role, chunk_number, included):
    return {
        "id": event_id,
        "role": role,
        "chunk_number": chunk_number,
        "included_entries": included,
    }


# --- pure helper: reality warnings ------------------------------------------

def test_reality_mismatch_produces_advisory_warning():
    events = [_event("e1", "coder", 1, [_entry("f1", "Project uses MongoDB.")])]
    analysis = analyze_injection_events(
        events, repo_signals={"db_engine": "postgresql"}
    )
    assert analysis.reality_signal_available is True
    assert analysis.reality_warning_count == 1
    warning = analysis.reality_warnings[0]
    assert warning.warning_type == "reality_mismatch_candidate"
    assert warning.status == "mismatch"
    assert warning.dimension == "db_engine"
    assert warning.memory_value == "mongodb"
    assert warning.repo_value == "postgresql"
    assert warning.advisory_only is True
    assert warning.fact_id == "f1"
    assert warning.role == "coder"
    assert warning.chunk_number == 1
    # Non-scary, no "truth"/"resolved"/"latest wins" wording.
    lowered = warning.reason.lower()
    assert "may be outdated" in lowered
    for banned in ("latest wins", "resolved", "truth"):
        assert banned not in lowered


def test_reality_match_produces_no_warning():
    events = [_event("e1", "coder", 1, [_entry("f1", "Project uses PostgreSQL.")])]
    analysis = analyze_injection_events(
        events, repo_signals={"db_engine": "postgresql"}
    )
    assert analysis.reality_signal_available is True
    assert analysis.reality_warning_count == 0
    assert analysis.reality_warnings == ()


def test_reality_unknown_memory_value_produces_no_warning():
    # "SQLAlchemy" is an ORM, not a recognizable engine value -> unknown.
    events = [_event("e1", "coder", 1, [
        _entry("f1", "Backend uses SQLAlchemy for database access."),
    ])]
    analysis = analyze_injection_events(
        events, repo_signals={"db_engine": "postgresql"}
    )
    assert analysis.reality_signal_available is True
    assert analysis.reality_warnings == ()


def test_unsupported_dimension_produces_no_warning():
    events = [_event("e1", "coder", 1, [_entry("f1", "Project uses MongoDB.")])]
    analysis = analyze_injection_events(
        events, repo_signals={"made_up_dimension": "whatever"}
    )
    # A usable (non-empty) signal was passed, but the dimension is unsupported,
    # so check_fact_against_signal returns unsupported -> no warning.
    assert analysis.reality_signal_available is True
    assert analysis.reality_warnings == ()


def test_no_signal_means_no_warning_and_unavailable():
    events = [_event("e1", "coder", 1, [_entry("f1", "Project uses MongoDB.")])]
    assert analyze_injection_events(events).reality_signal_available is False
    assert analyze_injection_events(events).reality_warnings == ()
    # Empty / falsy signal values are treated as no signal.
    empty = analyze_injection_events(events, repo_signals={"db_engine": ""})
    assert empty.reality_signal_available is False
    assert empty.reality_warnings == ()


def test_same_mismatching_fact_across_roles_is_one_warning():
    events = [
        _event("e1", "planner", 1, [_entry("f1", "Project uses MongoDB.")]),
        _event("e2", "coder", 1, [_entry("f1", "Project uses MongoDB.")]),
    ]
    analysis = analyze_injection_events(
        events, repo_signals={"db_engine": "postgresql"}
    )
    assert analysis.distinct_fact_count == 1
    assert analysis.reality_warning_count == 1


def test_duplicate_and_supersession_analysis_still_work_with_signal():
    events = [_event("e1", "planner", 1, [
        _entry("f1", "Tests use pytest.", category="test"),
        _entry("f2", "Run tests with pytest.", category="test"),
        _entry("f3", "Project uses MongoDB."),
    ])]
    analysis = analyze_injection_events(
        events, repo_signals={"db_engine": "postgresql"}
    )
    assert analysis.duplicate_candidate_count >= 1
    assert analysis.reality_warning_count == 1


# --- endpoint + real repo integration ---------------------------------------

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

    def create_project(repo_path: Path) -> str:
        response = client.post("/projects", json={
            "name": f"RealityWarn {uuid.uuid4()}",
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
            conn.execute(
                text("DELETE FROM memory_facts WHERE project_id = :p"),
                {"p": project_id},
            )
            conn.execute(
                text("DELETE FROM memory_injection_events WHERE project_id = :p"),
                {"p": project_id},
            )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE project_id = :p"),
                {"p": project_id},
            )


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _insert_run(run_id: str, project_id: str) -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs (id, project_id, feature_description, status)
            VALUES (:id, :project_id, :feature, 'running')
        """), {"id": run_id, "project_id": project_id, "feature": "test feature"})


def test_endpoint_surfaces_reality_mismatch_without_mutating_memory(
    repo, project_factory, client
):
    # Repo says MongoDB; injected memory says PostgreSQL -> advisory mismatch.
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact = add_fact(project_id, "Project uses PostgreSQL.", category="db", scope="backend")
    run_id = f"rw-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    record_memory_injection_event(
        run_id=run_id, project_id=project_id, role="coder", chunk_number=1,
        token_budget=1200, category_policy=["db"],
        included_entries=[_entry(fact["id"], "Project uses PostgreSQL.")],
    )

    resp = client.get(f"/api/v1/runs/{run_id}/memory-injections/analysis")
    assert resp.status_code == 200
    analysis = resp.json()["analysis"]

    assert analysis["reality_signal_available"] is True
    assert analysis["reality_warning_count"] == 1
    warning = analysis["reality_warnings"][0]
    assert warning["warning_type"] == "reality_mismatch_candidate"
    assert warning["status"] == "mismatch"
    assert warning["memory_value"] == "postgresql"
    assert warning["repo_value"] == "mongodb"
    assert warning["advisory_only"] is True

    # Critical: the analysis endpoint detected a mismatch but did NOT mutate the
    # fact (unlike /memory/verify-repo). Memory lifecycle stays human-controlled.
    stored = next(f for f in list_facts(project_id) if f["id"] == fact["id"])
    assert stored["status"] == "active"
    assert stored["is_stale"] in (0, False)


def test_endpoint_no_warning_when_repo_signal_unknown(
    repo, project_factory, client
):
    _write(repo, "README.md", "# docs only, no manifests")
    project_id = project_factory(repo)
    fact = add_fact(project_id, "Project uses PostgreSQL.", category="db")
    run_id = f"rw-unknown-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    record_memory_injection_event(
        run_id=run_id, project_id=project_id, role="coder", chunk_number=1,
        token_budget=1200, category_policy=["db"],
        included_entries=[_entry(fact["id"], "Project uses PostgreSQL.")],
    )

    analysis = client.get(
        f"/api/v1/runs/{run_id}/memory-injections/analysis"
    ).json()["analysis"]

    assert analysis["reality_signal_available"] is False
    assert analysis["reality_warning_count"] == 0
    assert analysis["reality_warnings"] == []


def test_endpoint_ambiguous_repo_signal_does_not_warn(
    repo, project_factory, client
):
    # Two distinct engines -> fingerprint is ambiguous -> no signal -> no warning.
    _write(repo, "requirements.txt", "psycopg2\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact = add_fact(project_id, "Project uses MySQL.", category="db")
    run_id = f"rw-amb-{uuid.uuid4().hex}"
    _insert_run(run_id, project_id)
    record_memory_injection_event(
        run_id=run_id, project_id=project_id, role="coder", chunk_number=1,
        token_budget=1200, category_policy=["db"],
        included_entries=[_entry(fact["id"], "Project uses MySQL.")],
    )

    analysis = client.get(
        f"/api/v1/runs/{run_id}/memory-injections/analysis"
    ).json()["analysis"]

    assert analysis["reality_signal_available"] is False
    assert analysis["reality_warnings"] == []
    # And the fact is untouched.
    stored = next(f for f in list_facts(project_id) if f["id"] == fact["id"])
    assert stored["status"] == "active"
