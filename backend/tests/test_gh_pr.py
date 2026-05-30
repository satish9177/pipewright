"""
test_gh_pr.py
Tests for GitHub CLI PR helpers. No real gh or network is used: gh detection
and subprocess.run are mocked.
"""

from types import SimpleNamespace

import pytest

from backend.git import gh_pr

pytestmark = pytest.mark.unit


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# ensure_gh_ready
# ---------------------------------------------------------------------------

def test_ensure_gh_ready_ok(monkeypatch):
    monkeypatch.setattr(gh_pr, "is_gh_installed", lambda: True)
    monkeypatch.setattr(gh_pr, "is_gh_authenticated", lambda: True)
    # Should not raise.
    gh_pr.ensure_gh_ready()


def test_ensure_gh_ready_raises_when_not_installed(monkeypatch):
    monkeypatch.setattr(gh_pr, "is_gh_installed", lambda: False)
    monkeypatch.setattr(gh_pr, "is_gh_authenticated", lambda: True)
    with pytest.raises(gh_pr.GhCliError, match="gh auth login"):
        gh_pr.ensure_gh_ready()


def test_ensure_gh_ready_raises_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(gh_pr, "is_gh_installed", lambda: True)
    monkeypatch.setattr(gh_pr, "is_gh_authenticated", lambda: False)
    with pytest.raises(gh_pr.GhCliError, match="not installed or not authenticated"):
        gh_pr.ensure_gh_ready()


# ---------------------------------------------------------------------------
# find_open_pr
# ---------------------------------------------------------------------------

def test_find_open_pr_returns_first(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        assert kwargs.get("shell", False) is False
        return _completed(
            stdout='[{"url":"https://github.com/a/b/pull/12","number":12,"title":"T"}]'
        )

    monkeypatch.setattr(gh_pr.subprocess, "run", fake_run)

    pr = gh_pr.find_open_pr("/repo", "pipewright/abc", "pipewright-staging")

    assert pr == {"url": "https://github.com/a/b/pull/12", "number": 12, "title": "T"}
    assert captured["args"][:2] == ["gh", "pr"]
    assert "--head" in captured["args"] and "pipewright/abc" in captured["args"]
    assert "--base" in captured["args"] and "pipewright-staging" in captured["args"]
    assert captured["cwd"] == "/repo"


def test_find_open_pr_returns_none_when_empty(monkeypatch):
    monkeypatch.setattr(gh_pr.subprocess, "run", lambda *a, **k: _completed(stdout="[]"))
    assert gh_pr.find_open_pr("/repo", "b", "base") is None


def test_find_open_pr_raises_and_sanitizes_on_failure(monkeypatch):
    secret = "ghp_" + "Z" * 40
    monkeypatch.setattr(
        gh_pr.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr=f"auth token={secret}"),
    )
    with pytest.raises(gh_pr.GhCliError) as exc:
        gh_pr.find_open_pr("/repo", "b", "base")
    assert secret not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

def test_create_pr_parses_url_and_number(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _completed(stdout="https://github.com/acme/demo/pull/77\n")

    monkeypatch.setattr(gh_pr.subprocess, "run", fake_run)

    pr = gh_pr.create_pr(
        "/repo", "pipewright/abc", "pipewright-staging", "Title", "Body"
    )

    assert pr == {
        "url": "https://github.com/acme/demo/pull/77",
        "number": 77,
        "title": "Title",
    }
    assert "create" in captured["args"]
    assert "--title" in captured["args"] and "Title" in captured["args"]


def test_create_pr_handles_extra_output_lines(monkeypatch):
    monkeypatch.setattr(
        gh_pr.subprocess,
        "run",
        lambda *a, **k: _completed(
            stdout="Warning: something\nhttps://github.com/acme/demo/pull/9\n"
        ),
    )
    pr = gh_pr.create_pr("/repo", "b", "base", "T", "B")
    assert pr["number"] == 9


def test_create_pr_raises_and_sanitizes_on_failure(monkeypatch):
    secret = "ghp_" + "Q" * 40
    monkeypatch.setattr(
        gh_pr.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr=f"failed token={secret}"),
    )
    with pytest.raises(gh_pr.GhCliError) as exc:
        gh_pr.create_pr("/repo", "b", "base", "T", "B")
    assert secret not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_create_pr_raises_when_url_unparseable(monkeypatch):
    monkeypatch.setattr(
        gh_pr.subprocess, "run", lambda *a, **k: _completed(stdout="no url here")
    )
    with pytest.raises(gh_pr.GhCliError, match="could not be parsed"):
        gh_pr.create_pr("/repo", "b", "base", "T", "B")


def test_run_gh_sanitizes_subprocess_exception(monkeypatch):
    secret = "ghp_" + "W" * 40

    def boom(*a, **k):
        raise TimeoutError(f"timed out token={secret}")

    monkeypatch.setattr(gh_pr.subprocess, "run", boom)
    with pytest.raises(gh_pr.GhCliError) as exc:
        gh_pr.find_open_pr("/repo", "b", "base")
    assert secret not in str(exc.value)
