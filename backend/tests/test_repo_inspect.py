"""
test_repo_inspect.py
Tests for read-only repo inspection: GitHub remote parsing and detect_repo.
detect_repo's low-level git/gh helpers are monkeypatched so tests never depend
on real git, a real gh install, or the network.
"""

import pytest

from backend.git import repo_inspect
from backend.git.repo_inspect import detect_repo, parse_github_remote

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_github_remote
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:owner/repo.git",
        "git@github.com:owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "ssh://git@github.com/owner/repo.git",
    ],
)
def test_parse_github_remote_supported_forms(url):
    assert parse_github_remote(url) == ("owner", "repo")


def test_parse_github_remote_is_case_insensitive_host():
    assert parse_github_remote("https://GitHub.com/owner/repo") == ("owner", "repo")


@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.com:owner/repo.git",
        "https://bitbucket.org/owner/repo.git",
        "https://example.com/owner/repo",
        "git@github.enterprise.com:owner/repo.git",
    ],
)
def test_parse_github_remote_non_github_returns_none(url):
    assert parse_github_remote(url) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "not a url",
        "https://github.com/owner",  # missing repo
        "git@github.com:owner",  # missing repo
        "https://github.com/",  # missing owner and repo
    ],
)
def test_parse_github_remote_malformed_returns_none(url):
    assert parse_github_remote(url) is None


# ---------------------------------------------------------------------------
# detect_repo
# ---------------------------------------------------------------------------

def _patch_detect(
    monkeypatch,
    *,
    git_root,
    origin_url,
    current_branch="feature",
    gh_installed=False,
    gh_authenticated=False,
):
    monkeypatch.setattr(repo_inspect, "get_git_root", lambda _p: git_root)
    monkeypatch.setattr(repo_inspect, "get_current_branch", lambda _p: current_branch)
    monkeypatch.setattr(repo_inspect, "get_origin_url", lambda _p: origin_url)
    monkeypatch.setattr(repo_inspect, "is_gh_installed", lambda: gh_installed)
    monkeypatch.setattr(repo_inspect, "is_gh_authenticated", lambda: gh_authenticated)


def test_detect_github_repo_gh_authenticated_recommends_github_cli(tmp_repo, monkeypatch):
    _patch_detect(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="git@github.com:owner/repo.git",
        gh_installed=True,
        gh_authenticated=True,
    )
    result = detect_repo(str(tmp_repo))

    assert result["is_git_repo"] is True
    assert result["path_is_git_root"] is True
    assert result["is_github_remote"] is True
    assert result["github_owner"] == "owner"
    assert result["github_repo"] == "repo"
    assert result["recommended_pr_mode"] == "github_cli"


def test_detect_github_repo_gh_missing_recommends_local_only(tmp_repo, monkeypatch):
    _patch_detect(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="https://github.com/owner/repo.git",
        gh_installed=False,
        gh_authenticated=False,
    )
    result = detect_repo(str(tmp_repo))

    assert result["is_github_remote"] is True
    assert result["gh_installed"] is False
    assert result["recommended_pr_mode"] == "local_only"


def test_detect_git_repo_no_origin_warns_local_only(tmp_repo, monkeypatch):
    _patch_detect(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url=None,
        gh_installed=True,
        gh_authenticated=True,
    )
    result = detect_repo(str(tmp_repo))

    assert result["is_git_repo"] is True
    assert result["is_github_remote"] is False
    assert result["recommended_pr_mode"] == "local_only"
    assert any("origin" in w for w in result["warnings"])


def test_detect_non_git_path_returns_false(tmp_repo, monkeypatch):
    _patch_detect(
        monkeypatch,
        git_root=None,
        origin_url=None,
    )
    result = detect_repo(str(tmp_repo))

    assert result["is_git_repo"] is False
    assert result["recommended_pr_mode"] == "local_only"
    assert any("not a Git repository" in w for w in result["warnings"])


def test_detect_subdirectory_returns_git_root(tmp_repo, monkeypatch):
    subdir = tmp_repo / "nested" / "pkg"
    subdir.mkdir(parents=True, exist_ok=True)
    _patch_detect(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="git@github.com:owner/repo.git",
        gh_installed=True,
        gh_authenticated=True,
    )
    result = detect_repo(str(subdir))

    assert result["is_git_repo"] is True
    assert result["git_root"] == str(tmp_repo.resolve())
    assert result["path_is_git_root"] is False
    assert any("subdirectory" in w for w in result["warnings"])


def test_detect_nonexistent_path_warns(monkeypatch):
    _patch_detect(monkeypatch, git_root=None, origin_url=None)
    result = detect_repo("/this/path/does/not/exist/pytest_tmp")

    assert result["is_git_repo"] is False
    assert any("does not exist" in w for w in result["warnings"])


def test_detect_never_recommends_manual_token(tmp_repo, monkeypatch):
    # Even with a full GitHub + gh setup, manual_token is never auto-recommended.
    _patch_detect(
        monkeypatch,
        git_root=str(tmp_repo),
        origin_url="git@github.com:owner/repo.git",
        gh_installed=True,
        gh_authenticated=True,
    )
    result = detect_repo(str(tmp_repo))
    assert result["recommended_pr_mode"] != "manual_token"
