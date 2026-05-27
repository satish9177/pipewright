"""
conftest.py
Pytest configuration and shared fixtures.

IMPORTANT:
  Tests run against the same SQLite database
  as the application (backend/db/pipewright.db).
  All test data must be cleaned up after
  each test session to avoid polluting the UI.

  Test projects are identified by repo_path
  containing '.pytest_tmp' - never use real
  repo paths in tests.
"""

import uuid
import shutil
import pytest
from pathlib import Path
from sqlalchemy import text
from backend.db.database import init_db, engine

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
    """Initialize database before any tests run."""
    init_db()


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    """
    Clean up all test data after the full test session.
    Runs AFTER all tests complete.
    Deletes any project whose repo_path contains .pytest_tmp.
    Also deletes pipeline_runs and checkpoints linked to
    those test projects.
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
