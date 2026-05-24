"""
test_local_git.py
Tests for safe local Git helper.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.git.local_git import (
    branch_exists_remote,
    branch_exists,
    checkout_file,
    commit,
    commit_files,
    create_or_checkout_branch,
    ensure_clean_worktree,
    ensure_git_repo,
    get_current_branch,
    get_current_hash,
    get_dirty_files,
    get_remote_url,
    is_working_tree_clean,
    normalize_git_path,
    push_branch,
    run_git,
    stage_files,
)

pytestmark = pytest.mark.unit


def git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    result = git(tmp_path, ["init"])
    assert result.returncode == 0, result.stderr
    assert git(tmp_path, ["config", "user.email", "test@example.com"]).returncode == 0
    assert git(tmp_path, ["config", "user.name", "Pipewright Test"]).returncode == 0
    return tmp_path


def initial_commit(repo_path: Path) -> str:
    (repo_path / "README.md").write_text("# Test\n", encoding="utf-8")
    assert git(repo_path, ["add", "--", "README.md"]).returncode == 0
    result = git(repo_path, ["commit", "-m", "initial"])
    assert result.returncode == 0, result.stderr
    return get_current_hash(str(repo_path))


def test_ensure_git_repo_passes_for_real_git_repo(git_repo):
    ensure_git_repo(str(git_repo))


def test_ensure_git_repo_fails_for_non_git_directory(tmp_path):
    with pytest.raises(RuntimeError, match=r"\[GIT\]"):
        ensure_git_repo(str(tmp_path))


def test_ensure_git_repo_rejects_subdirectory_inside_git_repo(git_repo):
    nested = git_repo / "nested"
    nested.mkdir()

    with pytest.raises(RuntimeError, match=r"\[GIT\]"):
        ensure_git_repo(str(nested))


def test_get_current_hash_returns_hash_after_initial_commit(git_repo):
    expected = initial_commit(git_repo)

    assert get_current_hash(str(git_repo)) == expected


def test_branch_exists_false_then_true_after_creation(git_repo):
    initial_commit(git_repo)

    assert branch_exists("feature/test", str(git_repo)) is False
    create_or_checkout_branch("feature/test", str(git_repo))
    assert branch_exists("feature/test", str(git_repo)) is True


def test_create_or_checkout_branch_creates_branch_if_missing(git_repo):
    initial_commit(git_repo)

    create_or_checkout_branch("feature/new", str(git_repo))

    assert get_current_branch(str(git_repo)) == "feature/new"


def test_create_or_checkout_branch_checks_out_existing_branch(git_repo):
    initial_commit(git_repo)
    create_or_checkout_branch("feature/existing", str(git_repo))
    create_or_checkout_branch("master", str(git_repo))

    create_or_checkout_branch("feature/existing", str(git_repo))

    assert get_current_branch(str(git_repo)) == "feature/existing"


def test_is_working_tree_clean_true_then_false_after_modification(git_repo):
    initial_commit(git_repo)

    assert is_working_tree_clean(str(git_repo)) is True
    (git_repo / "README.md").write_text("# Changed\n", encoding="utf-8")
    assert is_working_tree_clean(str(git_repo)) is False


def test_get_dirty_files_returns_normalized_modified_and_untracked(git_repo):
    initial_commit(git_repo)
    (git_repo / "README.md").write_text("# Changed\n", encoding="utf-8")
    nested = git_repo / "src" / "app.py"
    nested.parent.mkdir()
    nested.write_text("print('hello')\n", encoding="utf-8")

    dirty = get_dirty_files(str(git_repo))

    assert "README.md" in dirty
    assert "src/app.py" in dirty
    assert all("\\" not in path for path in dirty)


def test_stage_files_stages_only_provided_files(git_repo):
    initial_commit(git_repo)
    (git_repo / "one.txt").write_text("one\n", encoding="utf-8")
    (git_repo / "two.txt").write_text("two\n", encoding="utf-8")

    stage_files(["one.txt"], str(git_repo))
    result = git(git_repo, ["diff", "--cached", "--name-only"])

    assert result.stdout.splitlines() == ["one.txt"]


def test_stage_files_rejects_empty_file_list(git_repo):
    with pytest.raises(RuntimeError, match=r"\[GIT\]"):
        stage_files([], str(git_repo))


@pytest.mark.parametrize("unsafe_path", [
    "../secret.txt",
    "/absolute/path.txt",
    "..\\secret.txt",
])
def test_stage_files_rejects_unsafe_paths(git_repo, unsafe_path):
    with pytest.raises((RuntimeError, ValueError), match=r"\[SECURITY\]"):
        stage_files([unsafe_path], str(git_repo))


def test_commit_files_creates_commit_and_returns_hash(git_repo):
    initial_commit(git_repo)
    (git_repo / "feature.txt").write_text("feature\n", encoding="utf-8")

    commit_hash = commit_files(["feature.txt"], "add feature", str(git_repo))

    assert commit_hash == get_current_hash(str(git_repo))


def test_commit_failure_raises_runtime_error(git_repo):
    initial_commit(git_repo)

    with pytest.raises(RuntimeError, match=r"\[GIT\]"):
        commit("nothing staged", str(git_repo))


def test_checkout_file_restores_modified_file(git_repo):
    initial_commit(git_repo)
    readme = git_repo / "README.md"
    readme.write_text("# Changed\n", encoding="utf-8")

    checkout_file("README.md", str(git_repo))

    assert readme.read_text(encoding="utf-8") == "# Test\n"


def test_push_branch_uses_git_push_without_real_remote(git_repo):
    completed = subprocess.CompletedProcess(
        args=["git", "push"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with patch("backend.git.local_git.run_git", return_value=completed) as mocked:
        push_branch("feature/test", str(git_repo))

    mocked.assert_called_once_with(
        ["push", "-u", "origin", "feature/test"],
        str(git_repo),
    )


def test_branch_exists_remote_uses_ls_remote(git_repo):
    completed = subprocess.CompletedProcess(
        args=["git", "ls-remote"],
        returncode=0,
        stdout="abc\trefs/heads/feature/test\n",
        stderr="",
    )
    with patch("backend.git.local_git.run_git", return_value=completed) as mocked:
        assert branch_exists_remote(str(git_repo), "feature/test") is True

    mocked.assert_called_once_with(
        ["ls-remote", "--heads", "origin", "feature/test"],
        str(git_repo),
    )


def test_get_remote_url_returns_origin_url(git_repo):
    completed = subprocess.CompletedProcess(
        args=["git", "remote", "get-url", "origin"],
        returncode=0,
        stdout="https://github.com/acme/demo.git\n",
        stderr="",
    )
    with patch("backend.git.local_git.run_git", return_value=completed):
        assert get_remote_url(str(git_repo)) == "https://github.com/acme/demo.git"


def test_run_git_uses_cwd_repo_path(git_repo):
    completed = subprocess.CompletedProcess(
        args=["git", "status"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as mocked:
        result = run_git(["status"], str(git_repo))

    assert result is completed
    assert mocked.call_args.kwargs["cwd"] == str(git_repo.resolve())


def test_normalize_git_path_returns_forward_slashes():
    assert normalize_git_path("src\\app.py") == "src/app.py"


def test_ensure_clean_worktree_raises_with_dirty_files(git_repo):
    initial_commit(git_repo)
    (git_repo / "README.md").write_text("# Dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"\[GIT\].*README.md"):
        ensure_clean_worktree(str(git_repo))
