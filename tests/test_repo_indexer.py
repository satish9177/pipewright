"""
test_repo_indexer.py
Tests for Phase 2A zero-AI repository indexing.
Uses temporary repositories only.
"""

import json
import uuid
from pathlib import Path
from sqlalchemy import text

from backend.db.database import init_db, engine
from backend.repo.repo_indexer import (
    build_repo_index,
    classify_file,
    ensure_repo_indexed,
    extract_imports,
    get_relevant_files,
    save_file_index,
    scan_repo,
)


def write_file(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def clear_project_index(project_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM file_index
            WHERE project_id = :project_id
        """), {"project_id": project_id})


def make_sample_repo(tmp_path: Path) -> Path:
    write_file(tmp_path, "app/main.py", "from app.services.user_service import UserService\n")
    write_file(tmp_path, "app/services/user_service.py", "import os\n")
    write_file(tmp_path, "app/models/user.py", "class User:\n    pass\n")
    write_file(tmp_path, "tests/test_user.py", "def test_user():\n    assert True\n")
    write_file(tmp_path, "package.json", '{"scripts": {"test": "echo ok"}}\n')
    write_file(tmp_path, "README.md", "# Test Repo\n")
    return tmp_path


def test_scan_repo_indexes_supported_files(tmp_path):
    repo = make_sample_repo(tmp_path)

    files = scan_repo(str(repo))
    paths = {file_data["path"] for file_data in files}

    assert "app/main.py" in paths
    assert "app/services/user_service.py" in paths
    assert "app/models/user.py" in paths
    assert "tests/test_user.py" in paths
    assert "package.json" in paths
    assert "README.md" in paths
    assert all("\\" not in path for path in paths)


def test_skip_ignored_directories(tmp_path):
    write_file(tmp_path, "node_modules/lib.js", "export const x = 1\n")
    write_file(tmp_path, ".git/config", "[core]\n")
    write_file(tmp_path, "venv/file.py", "print('skip')\n")
    write_file(tmp_path, "__pycache__/x.py", "print('skip')\n")
    write_file(tmp_path, "app/main.py", "print('keep')\n")

    files = scan_repo(str(tmp_path))
    paths = {file_data["path"] for file_data in files}

    assert paths == {"app/main.py"}


def test_classify_file_works():
    assert classify_file("app/services/user_service.py") == "service"
    assert classify_file("app/models/user.py") == "model"
    assert classify_file("tests/test_user.py") == "test"
    assert classify_file("app/routes/health.py") == "route"
    assert classify_file("backend/app/routers/workflows.py") == "route"
    assert classify_file("backend/app/routes/workflows.py") == "route"
    assert classify_file("README.md") == "doc"
    assert classify_file("docker-compose.yml") == "config"


def test_extract_imports_works():
    python_imports = extract_imports(
        "app/main.py",
        "import os\nfrom pathlib import Path\nprint('x')\n",
    )
    js_imports = extract_imports(
        "src/main.ts",
        "import React from 'react'\nconst fs = require('fs')\n",
    )
    java_imports = extract_imports(
        "src/Main.java",
        "import java.util.List;\npublic class Main {}\n",
    )

    assert "import os" in python_imports
    assert "from pathlib import Path" in python_imports
    assert "import React from 'react'" in js_imports
    assert "const fs = require('fs')" in js_imports
    assert "import java.util.List;" in java_imports


def test_save_file_index_persists_rows(tmp_path):
    init_db()
    project_id = f"test-{uuid.uuid4()}"
    repo = make_sample_repo(tmp_path)
    files = scan_repo(str(repo))

    try:
        saved = save_file_index(project_id, files)
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(*) FROM file_index
                WHERE project_id = :project_id
            """), {"project_id": project_id}).fetchone()

        assert saved == len(files)
        assert row[0] == len(files)
    finally:
        clear_project_index(project_id)


def test_build_repo_index_returns_complete_status(tmp_path):
    init_db()
    project_id = f"test-{uuid.uuid4()}"
    repo = make_sample_repo(tmp_path)

    try:
        result = build_repo_index(project_id, str(repo))

        assert result["project_id"] == project_id
        assert result["status"] == "complete"
        assert result["files_indexed"] > 0
    finally:
        clear_project_index(project_id)


def test_ensure_repo_indexed_runs_only_when_empty(tmp_path):
    init_db()
    project_id = f"test-{uuid.uuid4()}"
    repo = make_sample_repo(tmp_path)

    try:
        first = ensure_repo_indexed(project_id, str(repo))
        second = ensure_repo_indexed(project_id, str(repo))

        assert first["status"] == "complete"
        assert second["status"] == "already_indexed"
        assert second["files_indexed"] == first["files_indexed"]
    finally:
        clear_project_index(project_id)


def test_get_relevant_files_returns_matching_files(tmp_path):
    init_db()
    project_id = f"test-{uuid.uuid4()}"
    repo = make_sample_repo(tmp_path)

    try:
        build_repo_index(project_id, str(repo))
        matches = get_relevant_files(project_id, "user service", limit=3)

        assert matches
        assert matches[0]["path"] == "app/services/user_service.py"
        assert matches[0]["score"] > 0
        assert json.loads(matches[0]["key_imports"]) == ["import os"]
    finally:
        clear_project_index(project_id)
