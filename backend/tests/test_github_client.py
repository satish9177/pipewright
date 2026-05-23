"""
test_github_client.py
Tests for github_client.py
No real GitHub API calls.
Uses unittest.mock to mock PyGithub.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from backend.github.github_client import (
    _generate_branch_name,
    _get_client
)

pytestmark = pytest.mark.unit


def test_generate_branch_name_format():
    name = _generate_branch_name("Add ping endpoint")
    assert name.startswith("pipewright/")
    assert "add" in name
    assert "ping" in name
    assert len(name) <= 80


def test_generate_branch_name_removes_special_chars():
    name = _generate_branch_name("Add /api/v1/ping! endpoint")
    assert "!" not in name
    assert "/" not in name.replace("pipewright/", "")


def test_generate_branch_name_is_unique():
    name1 = _generate_branch_name("Add ping endpoint")
    time.sleep(1)
    name2 = _generate_branch_name("Add ping endpoint")
    assert name1 != name2


def test_get_client_invalid_token():
    mock_client = MagicMock()
    mock_client.get_user.side_effect = Exception("bad credentials")

    with patch("backend.github.github_client.Github", return_value=mock_client):
        with pytest.raises(RuntimeError):
            _get_client("invalid-token-that-will-fail")
