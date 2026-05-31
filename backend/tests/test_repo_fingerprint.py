"""
test_repo_fingerprint.py
Unit tests for the shared deterministic repo fingerprint extractor
(backend/repo/repo_fingerprint.py). DB-only slice (M1.5 PR #16B).

Pure extraction: no DB, no AI, no network. Uses temp directories only.
"""

import shutil
import uuid
from pathlib import Path

import pytest

from backend.repo import repo_fingerprint
from backend.repo.repo_fingerprint import (
    build_repo_fingerprint,
    collect_env_example_var_names,
    detect_db_signals,
    discover_manifest_files,
    load_repo_file,
)

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def repo():
    root = LOCAL_TMP / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- DB engine detection per source ----------------------------------------

def test_db_signal_from_requirements_txt_postgres(repo):
    _write(repo, "requirements.txt", "fastapi\npsycopg2-binary\nsqlalchemy\n")
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "postgresql"
    assert fp.db.category == "db"
    assert fp.db.evidence_path == "requirements.txt"
    assert fp.db_ambiguous is False


def test_db_signal_from_package_json_mongo(repo):
    _write(
        repo,
        "package.json",
        '{"dependencies": {"express": "^4.18.0", "mongodb": "^6.0.0"}}',
    )
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "mongodb"
    assert fp.db.evidence_path == "package.json"


def test_db_signal_from_docker_compose_image(repo):
    _write(
        repo,
        "docker-compose.yml",
        "services:\n  db:\n    image: postgres:16\n",
    )
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "postgresql"
    assert fp.db.evidence_path == "docker-compose.yml"


def test_db_signal_from_prisma_schema(repo):
    _write(
        repo,
        "prisma/schema.prisma",
        'datasource db {\n  provider = "mysql"\n  url = env("DATABASE_URL")\n}\n',
    )
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "mysql"
    assert fp.db.evidence_path == "prisma/schema.prisma"


def test_db_signal_sqlite_from_marker(repo):
    _write(repo, "requirements.txt", "sqlalchemy\naiosqlite\n")
    # 'aiosqlite' contains the 'sqlite' marker.
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "sqlite"


# --- .env.example: names only, never values --------------------------------

def test_env_example_uses_variable_names_only(repo):
    # The variable NAME implies Mongo; there is no DB driver anywhere else.
    _write(repo, ".env.example", "MONGO_URL=\nAPP_NAME=\n")
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "mongodb"
    assert fp.db.evidence_path == ".env.example"
    assert "value not read" in fp.db.evidence_excerpt


def test_env_example_value_does_not_create_signal(repo):
    # Variable NAME is generic; only the VALUE mentions postgres. The value must
    # never be read, so no DB signal should be produced.
    _write(repo, ".env.example", "DATABASE_URL=postgresql://user:pass@host/db\n")
    fp = build_repo_fingerprint(repo)
    assert fp.db is None
    assert fp.db_signals == ()


def test_env_file_is_never_read(repo):
    # A real .env with a secret value must never be opened. Provide no other DB
    # signal; the fingerprint must come back empty (the .env is ignored entirely).
    secret = "sk-thisisaverylongsecretkeyvalue"
    _write(repo, ".env", f"MONGO_URL=mongodb://{secret}@host/db\n")
    fp = build_repo_fingerprint(repo)
    assert fp.db is None
    names = collect_env_example_var_names(repo)
    assert names == []
    # Direct load attempt of .env is refused by path safety.
    assert load_repo_file(repo, ".env") is None


def test_evidence_excerpt_is_fixed_string_not_content(repo):
    secret = "psycopg2==SECRETPINNEDVERSIONsk-xxxxxxxxxxxxxxxxxxxx"
    _write(repo, "requirements.txt", f"{secret}\n")
    fp = build_repo_fingerprint(repo)
    assert fp.db is not None
    assert fp.db.value == "postgresql"
    # Evidence is a human-written fixed string, never the raw line.
    assert fp.db.evidence_excerpt == "Detected PostgreSQL dependency or reference."
    assert secret not in fp.db.evidence_excerpt


# --- ambiguity / unknown ----------------------------------------------------

def test_multiple_exclusive_db_signals_are_ambiguous(repo):
    _write(repo, "requirements.txt", "psycopg2\n")
    _write(repo, "package.json", '{"dependencies": {"mongodb": "^6.0.0"}}')
    fp = build_repo_fingerprint(repo)
    assert fp.db is None
    assert fp.db_ambiguous is True
    values = {s.value for s in fp.db_signals}
    assert values == {"postgresql", "mongodb"}


def test_unknown_repo_has_no_db_signal(repo):
    _write(repo, "README.md", "# Just docs, no manifests\n")
    fp = build_repo_fingerprint(repo)
    assert fp.db is None
    assert fp.db_signals == ()
    assert fp.db_ambiguous is False


def test_empty_repo_has_no_db_signal(repo):
    fp = build_repo_fingerprint(repo)
    assert fp.db is None
    assert fp.db_signals == ()
    assert fp.db_ambiguous is False


def test_nonexistent_path_returns_empty_fingerprint(repo):
    fp = build_repo_fingerprint(repo / "does_not_exist")
    assert fp.db is None
    assert fp.db_signals == ()
    assert fp.db_ambiguous is False


# --- safety: caps, traversal, skip dirs -------------------------------------

def test_path_traversal_is_not_loaded(repo):
    assert load_repo_file(repo, "../outside.txt") is None
    assert load_repo_file(repo, "../../etc/passwd") is None


def test_oversized_files_are_skipped(repo):
    _write(repo, "requirements.txt", "psycopg2\n" * 100)  # well over 10 bytes
    assert load_repo_file(repo, "requirements.txt", max_size=10) is None
    # Under a generous cap the same file loads fine.
    assert load_repo_file(repo, "requirements.txt", max_size=10_000) is not None


def test_heavy_dirs_are_skipped(repo):
    _write(repo, "node_modules/old/package.json", '{"dependencies": {"mongodb": "^6"}}')
    _write(repo, "venv/lib/requirements.txt", "psycopg2\n")
    discovered = discover_manifest_files(repo)
    assert discovered == []
    fp = build_repo_fingerprint(repo)
    assert fp.db is None


def test_depth_cap_respected(repo):
    # A depth-2 manifest should not be discovered when max_depth=1.
    _write(repo, "a/b/requirements.txt", "psycopg2\n")
    discovered = discover_manifest_files(repo, max_depth=1)
    assert "a/b/requirements.txt" not in discovered


def test_max_files_cap_respected(repo):
    for i in range(10):
        _write(repo, f"svc-{i}/requirements.txt", "fastapi\n")
    discovered = discover_manifest_files(repo, max_files=3)
    assert len(discovered) == 3


# --- detect_db_signals pure function ---------------------------------------

def test_detect_db_signals_first_evidence_wins():
    files = {
        "a/requirements.txt": "psycopg2",
        "b/requirements.txt": "asyncpg",
    }
    signals = detect_db_signals(files)
    assert len(signals) == 1
    assert signals[0].value == "postgresql"
    # First file in iteration order is attributed.
    assert signals[0].evidence_path == "a/requirements.txt"


def test_detect_db_signals_deterministic_order():
    files = {
        "package.json": '{"dependencies": {"mongodb": "1"}}',
        "requirements.txt": "psycopg2",
    }
    signals = detect_db_signals(files)
    # DB_ENGINE_ORDER puts postgresql before mongodb regardless of dict order.
    assert [s.value for s in signals] == ["postgresql", "mongodb"]
