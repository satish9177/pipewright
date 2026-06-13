"""
Tests for repo indexer safety filters.
"""

from pathlib import Path

import pytest

from backend.repo.repo_indexer import scan_repo, should_skip_path

pytestmark = pytest.mark.unit


def test_repo_indexer_blocks_real_env_and_allows_env_samples(tmp_repo):
    (tmp_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_repo / ".env.example").write_text("EXAMPLE=value\n", encoding="utf-8")
    (tmp_repo / ".env.sample").write_text("SAMPLE=value\n", encoding="utf-8")
    (tmp_repo / "secrets.json").write_text("{}\n", encoding="utf-8")
    (tmp_repo / "credentials.json").write_text("{}\n", encoding="utf-8")
    (tmp_repo / "secrets.properties").write_text("token=value\n", encoding="utf-8")
    (tmp_repo / "credentials.xml").write_text("<token />\n", encoding="utf-8")

    files = scan_repo(str(tmp_repo))
    paths = {file["path"] for file in files}

    assert ".env" not in paths
    assert "secrets.json" not in paths
    assert "credentials.json" not in paths
    assert "secrets.properties" not in paths
    assert "credentials.xml" not in paths
    assert ".env.example" in paths
    assert ".env.sample" in paths


@pytest.mark.parametrize(
    "ancestor",
    ["build", "target", "dist", "coverage", ".cache", "node_modules", "venv"],
)
def test_repo_indexer_indexes_repo_under_skip_named_ancestor(tmp_path, ancestor):
    repo = tmp_path / ancestor / "myrepo"
    target = repo / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    files = scan_repo(str(repo))
    paths = {file["path"] for file in files}

    assert "src/app.py" in paths
    assert should_skip_path(target, repo_root=repo) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/main.rs",
        "cmd/server/main.go",
        "build.gradle",
        "gradle.properties",
        "pom.xml",
        "index.html",
        "styles/site.css",
        "frontend/App.vue",
        "frontend/Widget.svelte",
        "app/models/user.rb",
        "src/App.cs",
        "public/index.php",
        "src/Main.kt",
        "build.gradle.kts",
        "Sources/App.swift",
        "src/main/scala/App.scala",
        "styles/theme.scss",
        "styles/theme.sass",
    ],
)
def test_repo_indexer_indexes_common_multilanguage_extensions(tmp_repo, path):
    target = tmp_repo / Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// indexed text\n", encoding="utf-8")

    files = scan_repo(str(tmp_repo))
    paths = {file["path"] for file in files}

    assert path in paths


@pytest.mark.parametrize("path", ["app.py", "frontend/App.ts", "src/Main.java"])
def test_repo_indexer_preserves_existing_supported_extensions(tmp_repo, path):
    target = tmp_repo / Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("indexed text\n", encoding="utf-8")

    files = scan_repo(str(tmp_repo))
    paths = {file["path"] for file in files}

    assert path in paths


@pytest.mark.parametrize(
    "dirname",
    ["dist", "build", "target", "coverage", ".cache", "node_modules", "venv"],
)
def test_repo_indexer_still_skips_generated_directory_names(tmp_repo, dirname):
    target = tmp_repo / dirname / "file.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")

    files = scan_repo(str(tmp_repo))
    paths = {file["path"] for file in files}

    assert f"{dirname}/file.py" not in paths
    assert should_skip_path(target) is True
    assert should_skip_path(target, repo_root=tmp_repo) is True


@pytest.mark.parametrize(
    "filename",
    ["distance.py", "targets.py", "coverage_report.py", "build_tools.py"],
)
def test_skip_names_do_not_match_file_name_substrings(tmp_repo, filename):
    target = tmp_repo / filename
    target.write_text("x = 1\n", encoding="utf-8")

    files = scan_repo(str(tmp_repo))
    paths = {file["path"] for file in files}

    assert filename in paths
    assert should_skip_path(target) is False
