"""
test_gh_cli.py
Tests for read-only GitHub CLI detection.
No real gh or network is required - shutil.which and subprocess.run are mocked.
"""

from types import SimpleNamespace

import pytest

from backend.git import gh_cli

pytestmark = pytest.mark.unit


def test_is_gh_installed_true(monkeypatch):
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: "/usr/bin/gh")
    assert gh_cli.is_gh_installed() is True


def test_is_gh_installed_false(monkeypatch):
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: None)
    assert gh_cli.is_gh_installed() is False


def test_is_gh_authenticated_true(monkeypatch):
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        gh_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert gh_cli.is_gh_authenticated() is True


def test_is_gh_authenticated_false_when_gh_missing(monkeypatch):
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: None)

    def _should_not_run(*_a, **_k):
        raise AssertionError("subprocess.run must not be called when gh missing")

    monkeypatch.setattr(gh_cli.subprocess, "run", _should_not_run)
    assert gh_cli.is_gh_authenticated() is False


def test_is_gh_authenticated_false_on_auth_failure(monkeypatch):
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        gh_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="not logged in"),
    )
    assert gh_cli.is_gh_authenticated() is False


def test_is_gh_authenticated_false_on_exception(monkeypatch):
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: "/usr/bin/gh")

    def _boom(*_a, **_k):
        raise TimeoutError("gh timed out")

    monkeypatch.setattr(gh_cli.subprocess, "run", _boom)
    assert gh_cli.is_gh_authenticated() is False
