"""
Tests for repo indexer safety filters.
"""

import pytest

from backend.repo.repo_indexer import scan_repo

pytestmark = pytest.mark.unit


def test_repo_indexer_blocks_real_env_and_allows_env_samples(tmp_repo):
    (tmp_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_repo / ".env.example").write_text("EXAMPLE=value\n", encoding="utf-8")
    (tmp_repo / ".env.sample").write_text("SAMPLE=value\n", encoding="utf-8")

    files = scan_repo(str(tmp_repo))
    paths = {file["path"] for file in files}

    assert ".env" not in paths
    assert ".env.example" in paths
    assert ".env.sample" in paths
