"""
test_detect_endpoint.py
Tests for the read-only POST /projects/detect endpoint.
Git and gh helpers are monkeypatched so no real git/gh/network is needed.
"""

import pytest
from fastapi.testclient import TestClient

from backend.git import repo_inspect
from backend.main import app

pytestmark = pytest.mark.unit

client = TestClient(app)


def _patch(monkeypatch, *, git_root, origin_url, gh_installed, gh_authenticated):
    monkeypatch.setattr(repo_inspect, "get_git_root", lambda _p: git_root)
    monkeypatch.setattr(repo_inspect, "get_current_branch", lambda _p: "feature")
    monkeypatch.setattr(repo_inspect, "get_origin_url", lambda _p: origin_url)
    monkeypatch.setattr(repo_inspect, "is_gh_installed", lambda: gh_installed)
    monkeypatch.setattr(repo_inspect, "is_gh_authenticated", lambda: gh_authenticated)


def test_detect_endpoint_github_cli(tmp_repo, monkeypatch):
    _patch(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="git@github.com:owner/repo.git",
        gh_installed=True,
        gh_authenticated=True,
    )
    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})

    assert response.status_code == 200
    body = response.json()
    assert body["is_git_repo"] is True
    assert body["is_github_remote"] is True
    assert body["github_owner"] == "owner"
    assert body["github_repo"] == "repo"
    assert body["recommended_pr_mode"] == "github_cli"


def test_detect_endpoint_local_only_when_gh_missing(tmp_repo, monkeypatch):
    _patch(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="https://github.com/owner/repo.git",
        gh_installed=False,
        gh_authenticated=False,
    )
    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})

    assert response.status_code == 200
    assert response.json()["recommended_pr_mode"] == "local_only"


def test_detect_endpoint_non_git_path(tmp_repo, monkeypatch):
    _patch(
        monkeypatch,
        git_root=None,
        origin_url=None,
        gh_installed=False,
        gh_authenticated=False,
    )
    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})

    assert response.status_code == 200
    body = response.json()
    assert body["is_git_repo"] is False
    assert body["warnings"]


def test_detect_endpoint_rejects_blank_repo_path():
    response = client.post("/projects/detect", json={"repo_path": "   "})
    assert response.status_code == 422


def test_detect_endpoint_never_returns_token_field(tmp_repo, monkeypatch):
    _patch(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="git@github.com:owner/repo.git",
        gh_installed=True,
        gh_authenticated=True,
    )
    response = client.post("/projects/detect", json={"repo_path": str(tmp_repo)})
    body = response.json()
    assert "github_token" not in body
