"""
test_conflict_scope.py
Unit tests for the pure DB-sensitive run scope classifier (M1.5 PR #16D-2).

Pure helper: no DB, no filesystem, no network. Verifies deterministic
classification of files_expected only.
"""

import pytest

from backend.memory.conflict_scope import (
    get_db_sensitivity_reason,
    is_db_sensitive_run,
)

pytestmark = pytest.mark.unit


# --- True cases (strong DB indicators) -------------------------------------

DB_SENSITIVE_PATHS = [
    "backend/models/user.py",
    "backend/model/user.py",
    "backend/migrations/001_init.py",
    "alembic/versions/001.py",
    "prisma/schema.prisma",
    "backend/db/session.py",
    "backend/database/connection.py",
    "backend/repositories/user_repository.py",
    "backend/repository/user.py",
    "backend/schemas/user.py",
    "backend/schema/user.py",
    "backend/entities/user.py",
    "backend/entity/user.py",
    "backend/queries/user_queries.sql",
    "backend/query/lookup.py",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "alembic.ini",
]


@pytest.mark.parametrize("path", DB_SENSITIVE_PATHS)
def test_db_sensitive_paths_return_true(path):
    assert is_db_sensitive_run([path]) is True
    assert get_db_sensitivity_reason(path) is not None


# --- False cases (non-sensitive) -------------------------------------------

NON_SENSITIVE_PATHS = [
    "README.md",
    "docs/architecture.md",
    "frontend/src/App.tsx",
    "frontend/src/styles.css",
    "backend/routes/users.py",
    "backend/services/user_service.py",
    "backend/controllers/user_controller.py",
    "tests/test_utils.py",
    "tests/test_models.py",          # a test file, not a model (no 'models' segment)
    "backend/dbutils/helpers.py",    # 'dbutils' must not match the 'db' segment
    "src/main.py",
    "config/logging.yaml",           # generic config, not a DB manifest
]


@pytest.mark.parametrize("path", NON_SENSITIVE_PATHS)
def test_non_sensitive_paths_return_false(path):
    assert is_db_sensitive_run([path]) is False
    assert get_db_sensitivity_reason(path) is None


# --- normalization: windows / leading ./ / mixed case ----------------------

def test_windows_separators_normalize():
    assert is_db_sensitive_run(["backend\\models\\User.py"]) is True
    assert is_db_sensitive_run(["backend\\migrations\\001_init.py"]) is True
    assert is_db_sensitive_run(["frontend\\src\\App.tsx"]) is False


def test_leading_dot_slash_is_stripped():
    assert is_db_sensitive_run(["./backend/models/user.py"]) is True
    assert is_db_sensitive_run([".\\backend\\models\\user.py"]) is True
    assert is_db_sensitive_run(["./README.md"]) is False


def test_mixed_case_paths():
    assert is_db_sensitive_run(["Backend/Models/User.py"]) is True
    assert is_db_sensitive_run(["BACKEND/MIGRATIONS/001.PY"]) is True
    assert is_db_sensitive_run(["Frontend/Src/App.Tsx"]) is False


# --- list semantics ---------------------------------------------------------

def test_empty_list_is_false():
    assert is_db_sensitive_run([]) is False


def test_one_sensitive_among_many_is_true():
    files = [
        "README.md",
        "frontend/src/App.tsx",
        "backend/models/user.py",  # the one strong indicator
        "docs/guide.md",
    ]
    assert is_db_sensitive_run(files) is True


def test_all_non_sensitive_is_false():
    files = ["README.md", "frontend/src/App.tsx", "backend/routes/users.py"]
    assert is_db_sensitive_run(files) is False


def test_duplicates_do_not_matter():
    assert is_db_sensitive_run(["backend/models/user.py"] * 5) is True
    assert is_db_sensitive_run(["README.md", "README.md"]) is False


# --- defensive / invalid input ---------------------------------------------

def test_none_input_is_false():
    assert is_db_sensitive_run(None) is False


def test_non_list_input_is_false():
    assert is_db_sensitive_run("backend/models/user.py") is False


def test_non_string_items_are_skipped():
    assert is_db_sensitive_run([None, 123, {"x": 1}]) is False
    # A valid sensitive path still counts even with junk alongside it.
    assert is_db_sensitive_run([None, "backend/models/user.py", 123]) is True


def test_reason_never_raises_on_non_string():
    assert get_db_sensitivity_reason(None) is None
    assert get_db_sensitivity_reason("") is None


# --- purity / reason strings ------------------------------------------------

def test_helper_does_not_mutate_input():
    files = ["backend/models/user.py", "README.md"]
    snapshot = list(files)
    is_db_sensitive_run(files)
    assert files == snapshot


def test_reason_strings_for_debug():
    assert get_db_sensitivity_reason("requirements.txt") == "manifest:requirements.txt"
    assert get_db_sensitivity_reason("backend/migrations/001.py") == "classify:migration"
    assert get_db_sensitivity_reason("backend/schemas/user.py") == "db-path:schemas"
    assert get_db_sensitivity_reason("backend/queries/q.sql") in {
        "classify:migration",  # .sql -> migration via classify_file
        "db-path:queries",
    }
