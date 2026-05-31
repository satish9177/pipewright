"""
test_conflict_scope_warning.py
Tests for the non-blocking DB memory conflict warning surface (M1.5 PR #16D-3).

Covers backend/pipeline/chunked_orchestrator._emit_db_conflict_warning: it emits a
run `log` event on a conflict, never blocks, never mutates memory, never creates a
gate. Uses temp repos + the event bus; no real pipeline run needed.
"""

import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.events.event_bus import clear_all_events_for_tests, get_buffered_events
from backend.memory.memory_store import add_fact, list_facts
from backend.pipeline.chunked_orchestrator import _emit_db_conflict_warning
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"

DB_SENSITIVE_FILES = ["backend/models/user.py"]
NON_SENSITIVE_FILES = ["README.md"]


@pytest.fixture()
def repo():
    root = LOCAL_TMP / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def project_factory():
    project_ids: list[str] = []

    def make(repo_path: Path) -> str:
        project = create_project(
            name=f"WarnProj {uuid.uuid4()}",
            repo_path=str(repo_path),
            test_command="python --version",
        )
        project_ids.append(project["id"])
        return project["id"]

    yield make

    with engine.begin() as conn:
        for project_id in project_ids:
            conn.execute(
                text("DELETE FROM memory_facts WHERE project_id = :p"),
                {"p": project_id},
            )


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _add_db_fact(project_id: str, content: str) -> str:
    fact = add_fact(
        project_id=project_id, content=content, category="db", scope="backend",
        source="manual", added_by="test", approved_by="test",
    )
    return fact["id"]


def _conflict_events(run_id: str) -> list:
    return [
        event for event in get_buffered_events(run_id)
        if event.kind == "log"
        and isinstance(event.data, dict)
        and str(event.data.get("type", "")).startswith("memory_db_conflict")
    ]


def _gate_count(run_id: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM approval_gates WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()


# --- conflict + db-sensitive -> warning event, no mutation, no gate ---------

def test_conflict_and_db_sensitive_emits_warning(repo, project_factory):
    clear_all_events_for_tests()
    _write(repo, "requirements.txt", "pymongo\n")  # repo => mongodb
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    events = _conflict_events(run_id)
    assert len(events) == 1
    event = events[0]
    assert event.level == "warning"
    assert event.data["type"] == "memory_db_conflict"
    assert event.data["repo_db_signal"] == "mongodb"
    assert event.data["db_sensitive"] is True
    assert event.data["conflicts"][0]["fact_id"] == fact_id
    assert event.data["conflicts"][0]["repo_value"] == "mongodb"
    assert event.data["conflicts"][0]["memory_value"] == "postgresql"

    # No mutation: fact stays active and not stale.
    fact = next(f for f in list_facts(project_id) if f["id"] == fact_id)
    assert fact["status"] == "active"
    assert fact["is_stale"] in (0, False)
    # No approval gate created by a warning.
    assert _gate_count(run_id) == 0


# --- conflict + non-sensitive -> milder info, never warning -----------------

def test_conflict_and_non_sensitive_is_info_not_warning(repo, project_factory):
    clear_all_events_for_tests()
    _write(repo, "requirements.txt", "pymongo\n")
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses PostgreSQL.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), NON_SENSITIVE_FILES)

    events = _conflict_events(run_id)
    assert len(events) == 1
    assert events[0].level == "info"
    assert events[0].data["db_sensitive"] is False


# --- ambiguous repo signal -> info note, no block, no mutation --------------

def test_ambiguous_repo_signal_emits_info_only(repo, project_factory):
    clear_all_events_for_tests()
    _write(repo, "requirements.txt", "psycopg2\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    fact_id = _add_db_fact(project_id, "Project uses PostgreSQL.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    events = _conflict_events(run_id)
    assert len(events) == 1
    assert events[0].level == "info"
    assert events[0].data["type"] == "memory_db_conflict_ambiguous"
    # No mutation.
    fact = next(f for f in list_facts(project_id) if f["id"] == fact_id)
    assert fact["status"] == "active"


def test_ambiguous_repo_signal_non_sensitive_emits_nothing(repo, project_factory):
    clear_all_events_for_tests()
    _write(repo, "requirements.txt", "psycopg2\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses PostgreSQL.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), NON_SENSITIVE_FILES)

    assert _conflict_events(run_id) == []


# --- unknown signal / no conflict -> no event -------------------------------

def test_unknown_repo_signal_emits_nothing(repo, project_factory):
    clear_all_events_for_tests()
    _write(repo, "README.md", "# docs only")
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses PostgreSQL.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    assert _conflict_events(run_id) == []


def test_no_conflict_emits_nothing(repo, project_factory):
    clear_all_events_for_tests()
    _write(repo, "requirements.txt", "pymongo\n")  # repo => mongodb
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses MongoDB.")  # memory agrees
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    assert _conflict_events(run_id) == []


# --- project isolation ------------------------------------------------------

def test_project_isolation(repo, project_factory):
    clear_all_events_for_tests()
    # Project B: matching repo + memory (no conflict).
    _write(repo, "requirements.txt", "pymongo\n")
    project_b = project_factory(repo)
    _add_db_fact(project_b, "Project uses MongoDB.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_b, str(repo), DB_SENSITIVE_FILES)

    # No event: project B's own memory agrees with its repo; project A is irrelevant.
    assert _conflict_events(run_id) == []


# --- evidence redaction: no .env values in events ---------------------------

def test_evidence_never_contains_env_values(repo, project_factory):
    clear_all_events_for_tests()
    secret = "sk-thisisaverylongsecretkeyvalue"
    _write(repo, ".env", f"MONGO_URL=mongodb://{secret}@host/db\n")
    _write(repo, "requirements.txt", "pymongo\n")
    project_id = project_factory(repo)
    _add_db_fact(project_id, "Project uses PostgreSQL.")
    run_id = str(uuid.uuid4())

    _emit_db_conflict_warning(run_id, project_id, str(repo), DB_SENSITIVE_FILES)

    for event in get_buffered_events(run_id):
        assert secret not in event.message
        assert secret not in str(event.data)


# --- never raises -----------------------------------------------------------

def test_helper_never_raises_on_bad_repo_path(project_factory, repo):
    clear_all_events_for_tests()
    project_id = project_factory(repo)
    run_id = str(uuid.uuid4())
    # Nonexistent path -> unknown signal -> no event, no exception.
    _emit_db_conflict_warning(run_id, project_id, str(repo / "missing"), DB_SENSITIVE_FILES)
    assert _conflict_events(run_id) == []
