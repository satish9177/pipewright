"""
conftest.py
Pytest configuration and shared fixtures.

IMPORTANT:
  Tests run against an ISOLATED temporary SQLite database, never the real
  local app DB (backend/db/pipewright.db). This module sets PIPEWRIGHT_DB_PATH
  to a throwaway temp file BEFORE importing backend.db.database, so the engine
  (which is bound at import time and captured by reference across the codebase)
  points at the temp DB for the whole test session. The temp DB is deleted at
  session end. This prevents cross-test bleed and keeps test runs from polluting
  a developer's real database.

  Test projects are identified by repo_path
  containing '.pytest_tmp' - never use real
  repo paths in tests.
"""

import os
import tempfile

# Must run before backend.db.database is imported (directly or transitively),
# otherwise the engine would already be bound to the real app DB. Respect an
# externally provided PIPEWRIGHT_DB_PATH (e.g. CI) instead of overriding it.
if not os.environ.get("PIPEWRIGHT_DB_PATH"):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="pipewright-test-db-")
    os.environ["PIPEWRIGHT_DB_PATH"] = os.path.join(_TEST_DB_DIR, "test_pipewright.db")
else:
    _TEST_DB_DIR = None

import uuid
import shutil
import pytest
from pathlib import Path
from sqlalchemy import text
from backend.db.database import init_db, engine, DB_PATH

_LLM_ENV_VARS = (
    "DEFAULT_LLM_PROVIDER", "DEFAULT_LLM_MODEL",
    "TRIAGE_LLM_PROVIDER", "TRIAGE_LLM_MODEL",
    "PLANNER_LLM_PROVIDER", "PLANNER_LLM_MODEL",
    "CODER_LLM_PROVIDER", "CODER_LLM_MODEL",
    "REVIEWER_LLM_PROVIDER", "REVIEWER_LLM_MODEL",
    "SUMMARY_LLM_PROVIDER", "SUMMARY_LLM_MODEL",
)


@pytest.fixture()
def clear_llm_env(monkeypatch):
    """Remove all LLM provider/model env vars so shell config cannot leak into tests."""
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture(scope="session", autouse=True)
def initialize_database():
    """Initialize database before any tests run.

    Also asserts the active engine is the isolated temp DB, not the real local
    app DB. This is a safety net: if the import-order contract ever breaks and
    the engine binds to backend/db/pipewright.db, the whole suite fails loudly
    here instead of silently polluting the developer's database.
    """
    assert Path(DB_PATH).name != "pipewright.db", (
        "Test DB isolation failed: engine is bound to the real app DB "
        f"({DB_PATH}). PIPEWRIGHT_DB_PATH must be set before backend.db.database "
        "is imported."
    )
    init_db()


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    """
    Clean up after the full test session. Runs AFTER all tests complete.

    The session DB is a throwaway temp file, so the whole thing is dropped at
    the end (after disposing the engine so the SQLite file is unlocked on
    Windows). The per-project row cleanup below is kept as a harmless defensive
    measure in case an externally provided PIPEWRIGHT_DB_PATH points somewhere
    that should outlive the run.
    """
    yield
    # Session is done - clean up test data
    try:
        with engine.connect() as conn:
            # Get test project IDs first
            result = conn.execute(text("""
                SELECT id FROM projects
                WHERE repo_path LIKE :pattern
            """), {"pattern": "%pytest_tmp%"})
            test_ids = [row[0] for row in result.fetchall()]

            if test_ids:
                # Delete related pipeline runs
                for pid in test_ids:
                    conn.execute(text("""
                        DELETE FROM pipeline_runs
                        WHERE project_id = :pid
                    """), {"pid": pid})

                # Delete test projects
                conn.execute(text("""
                    DELETE FROM projects
                    WHERE repo_path LIKE :pattern
                """), {"pattern": "%pytest_tmp%"})

                conn.commit()
                print(f"[conftest] Cleaned {len(test_ids)} test projects")
            else:
                print("[conftest] No test projects to clean")
    except Exception as e:
        print(f"[conftest] Cleanup warning: {e}")

    # Drop the throwaway session DB. Only remove the temp dir this module
    # created; never touch an externally supplied PIPEWRIGHT_DB_PATH.
    if _TEST_DB_DIR is not None:
        try:
            engine.dispose()
        except Exception as e:
            print(f"[conftest] Engine dispose warning: {e}")
        shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


@pytest.fixture()
def tmp_repo():
    """
    Create a fresh temp repo folder for each test.
    Always inside .pytest_tmp/ so cleanup can find it.
    Cleaned up after each individual test.
    """
    folder = LOCAL_TMP / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)
