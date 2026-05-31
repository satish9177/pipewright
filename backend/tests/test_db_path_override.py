"""
test_db_path_override.py
Regression tests for the PIPEWRIGHT_DB_PATH override (PR #15E).

The DB path / engine are resolved at import time in backend.db.database, so the
override behavior is exercised in a fresh subprocess with a controlled
environment. This keeps the active test-session engine (already bound to the
isolated temp DB by conftest) untouched.
"""

import os
import sys
import subprocess
from pathlib import Path

import pytest

from backend.db.database import DB_PATH as ACTIVE_DB_PATH

pytestmark = pytest.mark.unit

# backend/tests/test_db_path_override.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Imports backend.db.database fresh and prints the resolved DB_PATH and engine
# URL, one per line, so the parent test can assert on them.
_PROBE = (
    "from backend.db.database import DB_PATH, engine; "
    "print(DB_PATH); print(engine.url)"
)


def _run_probe(env_override: dict[str, str | None]) -> tuple[str, str]:
    """Run the probe in a subprocess with env tweaks; return (db_path, url)."""
    env = dict(os.environ)
    for key, value in env_override.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"probe failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 2, f"unexpected probe output: {result.stdout!r}"
    return lines[0].strip(), lines[1].strip()


def test_unset_env_uses_default_db_path():
    # Parent pytest process has PIPEWRIGHT_DB_PATH set (by conftest); the
    # subprocess must run with it removed to observe the default.
    db_path, url = _run_probe({"PIPEWRIGHT_DB_PATH": None})

    resolved = Path(db_path)
    assert resolved.name == "pipewright.db"
    assert resolved == (_REPO_ROOT / "backend" / "db" / "pipewright.db")
    assert "pipewright.db" in url


def test_env_override_uses_override_path(tmp_path):
    override = tmp_path / "custom_dir" / "custom.db"
    # Parent dir does not exist yet; database.py must create it.
    assert not override.parent.exists()

    db_path, url = _run_probe({"PIPEWRIGHT_DB_PATH": str(override)})

    assert Path(db_path) == override
    assert override.parent.is_dir()
    assert "custom.db" in url
    assert "pipewright.db" not in Path(db_path).name


def test_active_session_engine_is_isolated():
    # The engine the whole suite is running against must be the temp DB set up
    # by conftest, never the real local app DB.
    assert Path(ACTIVE_DB_PATH).name != "pipewright.db"
