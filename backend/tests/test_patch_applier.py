"""
test_patch_applier.py
Tests for patch_applier.py
No API calls. Pure file operations.
Uses local .pytest_tmp folder instead of
Windows system temp (which denies access).
"""

import uuid
import shutil
import subprocess
import pytest
from pathlib import Path
from pydantic import ValidationError
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline.patch_applier import (
    BACKUP_DIR,
    PatchApplyOutcome,
    actual_changed_files,
    apply_patch,
    apply_patch_guarded,
    classify_patch_failure,
    rollback_patch,
    validate_changed_files_in_scope,
)
from backend.pipeline.patch_failures import PatchFailureType

pytestmark = pytest.mark.unit


def test_filechange_edit_requires_old_and_new_string():
    with pytest.raises(ValidationError):
        FileChange(path="f.py", action="edit", reason="missing fields")
    with pytest.raises(ValidationError):
        FileChange(path="f.py", action="edit", old_string="x", reason="missing new")

    # Valid edit construction succeeds; create/modify unaffected.
    FileChange(path="f.py", action="edit", old_string="x", new_string="y", reason="ok")
    FileChange(path="f.py", action="create", content="x\n", reason="ok")

# Use local folder instead of Windows system temp
LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def tmp_repo():
    """
    Create a fresh temp repo folder for each test.
    Clean it up after test completes.
    """
    folder = LOCAL_TMP / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def _set_target_repo(monkeypatch, tmp_repo):
    from backend.config import keys
    import backend.pipeline.patch_applier as patch_applier

    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))
    monkeypatch.setattr(patch_applier, "save_checkpoint", lambda **kwargs: None)


def make_coder_output(run_id: str) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="Add health endpoint",
        files_changed=[
            FileChange(
                path="new_file.py",
                action="create",
                content="def health():\n    return {'status': 'ok'}\n",
                reason="New health check function"
            )
        ],
        summary="Created health check function"
    )


def test_apply_empty_files_changed_returns_failed_without_checkpoint(tmp_repo, monkeypatch):
    from backend.config import keys
    import backend.pipeline.patch_applier as patch_applier

    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))
    checkpoint_calls = []
    monkeypatch.setattr(
        patch_applier,
        "save_checkpoint",
        lambda **kwargs: checkpoint_calls.append(kwargs),
    )

    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="No-op",
        files_changed=[],
        summary="No changes",
    )

    result = apply_patch(output, run_id)

    assert result.success is False
    assert result.diff == ""
    assert result.files_applied == []
    assert result.rollback_available is False
    assert checkpoint_calls == []
    assert not (BACKUP_DIR / run_id / "manifest.json").exists()


def test_apply_creates_new_file(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())
    result = apply_patch(make_coder_output(run_id), run_id)

    assert result.success is True
    created = tmp_repo / "new_file.py"
    assert created.exists()
    assert "health" in created.read_text()


def test_apply_generates_diff(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())
    result = apply_patch(make_coder_output(run_id), run_id)

    assert result.diff is not None
    assert len(result.diff) > 0


def test_rollback_restores_file(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    original_file = tmp_repo / "existing.py"
    original_file.write_text("original content\n")

    run_id = str(uuid.uuid4())

    output = CoderHandoff(
        run_id=run_id,
        feature_description="Modify existing file",
        files_changed=[
            FileChange(
                path="existing.py",
                action="modify",
                content="modified content\n",
                reason="test modify"
            )
        ],
        summary="Modified existing file"
    )

    apply_patch(output, run_id)
    assert original_file.read_text() == "modified content\n"

    rollback_patch(run_id)
    assert original_file.read_text() == "original content\n"


def test_forbidden_path_rejected(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())

    output = CoderHandoff(
        run_id=run_id,
        feature_description="Try to modify .env",
        files_changed=[
            FileChange(
                path=".env",
                action="modify",
                content="HACKED=true\n",
                reason="malicious edit"
            )
        ],
        summary="Should be rejected"
    )

    with pytest.raises(RuntimeError):
        apply_patch(output, run_id)


def test_path_traversal_rejected(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())

    output = CoderHandoff(
        run_id=run_id,
        feature_description="Try path traversal",
        files_changed=[
            FileChange(
                path="../../../etc/passwd",
                action="modify",
                content="hacked\n",
                reason="path traversal attempt"
            )
        ],
        summary="Should be rejected"
    )

    with pytest.raises(RuntimeError):
        apply_patch(output, run_id)


def _edit_output(
    run_id: str,
    path: str,
    old_string: str,
    new_string: str,
) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="Targeted edit",
        files_changed=[
            FileChange(
                path=path,
                action="edit",
                old_string=old_string,
                new_string=new_string,
                reason="targeted edit",
            )
        ],
        summary="Applied a targeted edit",
    )


def test_edit_preserves_lf_line_endings(tmp_repo, monkeypatch):
    _set_target_repo(monkeypatch, tmp_repo)
    target = tmp_repo / "lf.py"
    target.write_bytes(b"alpha = 1\nbeta = 2\n")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(run_id, "lf.py", "beta = 2", "beta = 3"),
        run_id,
    )

    assert result.success is True
    updated = target.read_bytes()
    assert updated == b"alpha = 1\nbeta = 3\n"
    assert b"\r\n" not in updated


def test_edit_preserves_crlf_line_endings(tmp_repo, monkeypatch):
    _set_target_repo(monkeypatch, tmp_repo)
    target = tmp_repo / "crlf.py"
    target.write_bytes(b"alpha = 1\r\nbeta = 2\r\ngamma = 3\r\n")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(
            run_id,
            "crlf.py",
            "beta = 2\ngamma = 3",
            "beta = 4\ngamma = 3",
        ),
        run_id,
    )

    assert result.success is True
    updated = target.read_bytes()
    assert updated == b"alpha = 1\r\nbeta = 4\r\ngamma = 3\r\n"
    assert b"\n" not in updated.replace(b"\r\n", b"")


def test_create_defaults_to_lf_line_endings(tmp_repo, monkeypatch):
    _set_target_repo(monkeypatch, tmp_repo)
    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="Create LF file",
        files_changed=[
            FileChange(
                path="created.py",
                action="create",
                content="alpha = 1\r\nbeta = 2\rgamma = 3\n",
                reason="create",
            )
        ],
        summary="Created file",
    )

    result = apply_patch(output, run_id)

    assert result.success is True
    assert (tmp_repo / "created.py").read_bytes() == (
        b"alpha = 1\nbeta = 2\ngamma = 3\n"
    )


def test_edit_mixed_eol_file_does_not_normalize_whole_file(tmp_repo, monkeypatch):
    _set_target_repo(monkeypatch, tmp_repo)
    target = tmp_repo / "mixed.txt"
    target.write_bytes(b"first\r\nold\nthird\r\n")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(run_id, "mixed.txt", "old\nthird", "new\nthird"),
        run_id,
    )

    assert result.success is True
    assert target.read_bytes() == b"first\r\nnew\nthird\r\n"


def test_edit_large_readme_like_file_succeeds(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    # 238-line README, mirroring the real E2E failure case.
    lines = [f"Line {i}" for i in range(238)]
    lines[100] = "This has a teh typo to fix"
    readme = tmp_repo / "README.md"
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(
            run_id,
            "README.md",
            "This has a teh typo to fix",
            "This has a the typo to fix",
        ),
        run_id,
    )

    assert result.success is True
    updated = readme.read_text(encoding="utf-8")
    assert "the typo to fix" in updated
    assert "teh typo" not in updated
    # Only the targeted line changed; everything else is intact.
    assert "Line 0" in updated
    assert "Line 237" in updated
    assert len(updated.splitlines()) == 238


def test_edit_1000_line_file_small_change_succeeds(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    lines = [f"row_{i} = {i}" for i in range(1000)]
    big = tmp_repo / "big.py"
    big.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(run_id, "big.py", "row_500 = 500", "row_500 = 5000"),
        run_id,
    )

    assert result.success is True
    updated = big.read_text(encoding="utf-8")
    assert "row_500 = 5000\n" in updated
    assert len(updated.splitlines()) == 1000


def test_edit_fails_when_old_string_missing(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    target = tmp_repo / "file.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="old_string not found"):
        apply_patch(
            _edit_output(run_id, "file.py", "does_not_exist = 0", "x = 0"),
            run_id,
        )

    # File untouched.
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 2\n"


def test_edit_fails_when_old_string_appears_multiple_times(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    target = tmp_repo / "file.py"
    target.write_text("dup = 1\ndup = 1\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="not unique"):
        apply_patch(
            _edit_output(run_id, "file.py", "dup = 1", "dup = 2"),
            run_id,
        )

    # File untouched.
    assert target.read_text(encoding="utf-8") == "dup = 1\ndup = 1\n"


def test_edit_forbidden_path_blocked(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    (tmp_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="forbidden path"):
        apply_patch(
            _edit_output(run_id, ".env", "SECRET=value", "SECRET=hacked"),
            run_id,
        )

    assert (tmp_repo / ".env").read_text(encoding="utf-8") == "SECRET=value\n"


def test_edit_nonexistent_file_blocked(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="edit target missing"):
        apply_patch(
            _edit_output(run_id, "missing.py", "foo", "bar"),
            run_id,
        )


def test_edit_rollback_restores_file(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    target = tmp_repo / "existing.py"
    target.write_text("value = 1\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    apply_patch(
        _edit_output(run_id, "existing.py", "value = 1", "value = 2"),
        run_id,
    )
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    rollback_patch(run_id)
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_edit_diff_is_generated(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    target = tmp_repo / "existing.py"
    target.write_text("value = 1\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(run_id, "existing.py", "value = 1", "value = 2"),
        run_id,
    )

    assert "value = 1" in result.diff
    assert "value = 2" in result.diff


def test_edit_windows_separators_remain_safe(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    nested = tmp_repo / "src" / "app.py"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("config = 'old'\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(run_id, "src\\app.py", "config = 'old'", "config = 'new'"),
        run_id,
    )

    assert result.success is True
    assert nested.read_text(encoding="utf-8") == "config = 'new'\n"


def test_edit_noop_applies_with_empty_diff(tmp_repo, monkeypatch):
    """
    A no-op edit (new_string identical to old_string) still 'succeeds' at the
    patch layer and leaves the file byte-identical, producing an empty diff.
    No effective change is committed here — the commit guard in
    chunked_orchestrator is responsible for refusing to commit this state.
    """
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    target = tmp_repo / "file.py"
    target.write_text("value = 1\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    result = apply_patch(
        _edit_output(run_id, "file.py", "value = 1", "value = 1"),
        run_id,
    )

    assert result.success is True
    # File content unchanged and diff is empty -> no effective change.
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert result.diff == ""


def test_large_file_modify_with_full_content_rejected(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    large_file = tmp_repo / "README.md"
    large_file.write_text(
        "\n".join(f"Line {i}" for i in range(238)) + "\n",
        encoding="utf-8",
    )
    original = large_file.read_text(encoding="utf-8")

    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="Wholesale rewrite of a large file",
        files_changed=[
            FileChange(
                path="README.md",
                action="modify",
                content="brand new short content\n",
                reason="rewrite",
            )
        ],
        summary="Should be rejected",
    )

    with pytest.raises(RuntimeError, match="cannot be replaced wholesale"):
        apply_patch(output, run_id)

    # File untouched.
    assert large_file.read_text(encoding="utf-8") == original


def test_small_file_modify_still_works(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    small_file = tmp_repo / "small.py"
    small_file.write_text("old = 1\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="Rewrite a small file",
        files_changed=[
            FileChange(
                path="small.py",
                action="modify",
                content="new = 2\n",
                reason="small rewrite",
            )
        ],
        summary="Small modify",
    )

    result = apply_patch(output, run_id)
    assert result.success is True
    assert small_file.read_text(encoding="utf-8") == "new = 2\n"


def test_legacy_manifest_path_unchanged(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())
    apply_patch(make_coder_output(run_id), run_id, chunk_number=0)

    assert (BACKUP_DIR / run_id / "manifest.json").exists()


def test_chunk_manifest_path_is_scoped(tmp_repo, monkeypatch):
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))

    run_id = str(uuid.uuid4())
    output_one = CoderHandoff(
        run_id=run_id,
        feature_description="Create chunk one file",
        files_changed=[
            FileChange(
                path="chunk_one.py",
                action="create",
                content="x = 1\n",
                reason="chunk one"
            )
        ],
        summary="Created chunk one"
    )
    output_two = CoderHandoff(
        run_id=run_id,
        feature_description="Create chunk two file",
        files_changed=[
            FileChange(
                path="chunk_two.py",
                action="create",
                content="x = 2\n",
                reason="chunk two"
            )
        ],
        summary="Created chunk two"
    )

    apply_patch(output_one, run_id, chunk_number=1)
    apply_patch(output_two, run_id, chunk_number=2)

    chunk_one_manifest = BACKUP_DIR / run_id / "chunk_1" / "manifest.json"
    chunk_two_manifest = BACKUP_DIR / run_id / "chunk_2" / "manifest.json"

    assert chunk_one_manifest.exists()
    assert chunk_two_manifest.exists()
    assert "chunk_one.py" in chunk_one_manifest.read_text(encoding="utf-8")
    assert "chunk_two.py" in chunk_two_manifest.read_text(encoding="utf-8")
    assert "chunk_two.py" not in chunk_one_manifest.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Guarded patch application (#18C)
#
# These exercise apply_patch_guarded and its helpers. apply_patch_guarded is a
# pure mechanism and is NOT wired into the orchestrator/tester here.
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def git_repo(tmp_repo, monkeypatch):
    """A real, clean git repo with one seed commit, set as the target repo."""
    from backend.config import keys

    _git(tmp_repo, "init")
    _git(tmp_repo, "config", "user.email", "test@example.com")
    _git(tmp_repo, "config", "user.name", "Pipewright Test")
    (tmp_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_repo, "add", "-A")
    _git(tmp_repo, "commit", "-m", "seed")

    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))
    return tmp_repo


def _create_output(run_id: str, path: str, content: str = "x = 1\n") -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="Create a file",
        files_changed=[
            FileChange(path=path, action="create", content=content, reason="create")
        ],
        summary="create",
    )


# --- success path --------------------------------------------------------- #


def test_guarded_success_returns_patch_result(git_repo):
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _create_output(run_id, "new_file.py"),
        run_id,
        files_expected=["new_file.py"],
    )
    assert isinstance(outcome, PatchApplyOutcome)
    assert outcome.success is True
    assert outcome.failure is None
    assert outcome.patch_result is not None and outcome.patch_result.success is True
    assert (git_repo / "new_file.py").exists()


# --- precondition --------------------------------------------------------- #


def test_guarded_dirty_worktree_blocks_before_patch(git_repo):
    # Uncommitted change makes the tree dirty.
    (git_repo / "seed.txt").write_text("seed dirty\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _create_output(run_id, "new_file.py"),
        run_id,
        files_expected=["new_file.py"],
    )
    assert outcome.success is False
    assert outcome.failure.failure_type == PatchFailureType.DIRTY_WORKTREE
    # No write happened.
    assert not (git_repo / "new_file.py").exists()
    assert not (BACKUP_DIR / run_id / "manifest.json").exists()


# --- empty / no-op -> NO_CHANGES ------------------------------------------ #


def test_guarded_empty_changes_is_no_changes(git_repo):
    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="nothing",
        files_changed=[],
        summary="nothing",
    )
    outcome = apply_patch_guarded(output, run_id, files_expected=["x.py"])
    assert outcome.success is False
    assert outcome.failure.failure_type == PatchFailureType.NO_CHANGES


def test_guarded_noop_edit_is_no_changes_and_clean(git_repo):
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _edit_output(run_id, "seed.txt", "seed", "seed"),
        run_id,
        files_expected=["seed.txt"],
    )
    assert outcome.success is False
    assert outcome.failure.failure_type == PatchFailureType.NO_CHANGES
    assert outcome.failure.working_tree_clean is True
    assert _git(git_repo, "status", "--porcelain").stdout.strip() == ""


# --- apply-time classification -------------------------------------------- #


def test_guarded_malformed_action(git_repo):
    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="bad action",
        files_changed=[FileChange(path="x.py", action="frobnicate", reason="bad")],
        summary="bad",
    )
    outcome = apply_patch_guarded(output, run_id, files_expected=["x.py"])
    assert outcome.success is False
    assert outcome.failure.failure_type == PatchFailureType.PATCH_MALFORMED
    assert outcome.failure.working_tree_clean is True


def test_guarded_large_file_wholesale_modify_is_malformed(git_repo):
    big = git_repo / "BIG.md"
    big.write_text("\n".join(f"Line {i}" for i in range(238)) + "\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "add big")

    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="wholesale",
        files_changed=[
            FileChange(path="BIG.md", action="modify", content="tiny\n", reason="x")
        ],
        summary="x",
    )
    outcome = apply_patch_guarded(output, run_id, files_expected=["BIG.md"])
    assert outcome.failure.failure_type == PatchFailureType.PATCH_MALFORMED
    assert outcome.failure.working_tree_clean is True


def test_guarded_old_string_missing_is_does_not_apply(git_repo):
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _edit_output(run_id, "seed.txt", "not-present", "x"),
        run_id,
        files_expected=["seed.txt"],
    )
    assert outcome.failure.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY
    assert outcome.failure.working_tree_clean is True


def test_guarded_old_string_not_unique_is_does_not_apply(git_repo):
    dup = git_repo / "dup.txt"
    dup.write_text("dup\ndup\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "dup")

    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _edit_output(run_id, "dup.txt", "dup", "x"),
        run_id,
        files_expected=["dup.txt"],
    )
    assert outcome.failure.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY


def test_guarded_create_target_exists_is_does_not_apply(git_repo):
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _create_output(run_id, "seed.txt"),
        run_id,
        files_expected=["seed.txt"],
    )
    assert outcome.failure.failure_type == PatchFailureType.PATCH_DOES_NOT_APPLY


def test_guarded_forbidden_file(git_repo):
    # Commit the .env first so the tree is clean and the forbidden-write guard
    # (not the dirty-worktree precondition) is what rejects the edit.
    (git_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "add env")

    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _edit_output(run_id, ".env", "SECRET=value", "SECRET=hacked"),
        run_id,
        files_expected=[".env"],
    )
    assert outcome.failure.failure_type == PatchFailureType.FORBIDDEN_FILE
    assert (git_repo / ".env").read_text(encoding="utf-8") == "SECRET=value\n"


def test_guarded_target_missing(git_repo):
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _edit_output(run_id, "missing.py", "foo", "bar"),
        run_id,
        files_expected=["missing.py"],
    )
    assert outcome.failure.failure_type == PatchFailureType.TARGET_MISSING
    assert outcome.failure.working_tree_clean is True


# --- partial apply: no residue -------------------------------------------- #


def test_guarded_partial_apply_leaves_no_residue(git_repo):
    # First change is a valid create; second targets a missing file. Validation
    # rejects before any write, so the valid file must never appear.
    run_id = str(uuid.uuid4())
    output = CoderHandoff(
        run_id=run_id,
        feature_description="two changes",
        files_changed=[
            FileChange(path="a.py", action="create", content="a = 1\n", reason="ok"),
            FileChange(
                path="b.py", action="edit", old_string="x", new_string="y", reason="bad"
            ),
        ],
        summary="partial",
    )
    outcome = apply_patch_guarded(output, run_id, files_expected=["a.py", "b.py"])
    assert outcome.success is False
    assert outcome.failure.failure_type == PatchFailureType.TARGET_MISSING
    assert not (git_repo / "a.py").exists()
    assert _git(git_repo, "status", "--porcelain").stdout.strip() == ""


# --- post-apply scope validation ------------------------------------------ #


def test_guarded_post_apply_scope_violation_rolls_back(git_repo):
    # The coder creates a file that is NOT in files_expected. The patch applies,
    # then post-apply scope validation catches it and rolls back.
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _create_output(run_id, "sneaky.py"),
        run_id,
        files_expected=["allowed.py"],
    )
    assert outcome.success is False
    assert outcome.failure.failure_type == PatchFailureType.SCOPE_VIOLATION
    assert "sneaky.py" in outcome.failure.changed_files_actual
    assert outcome.failure.allowed_files == ["allowed.py"]
    assert outcome.failure.rollback_performed is True
    assert outcome.failure.working_tree_clean is True
    # Rolled back: the out-of-scope file is gone and the tree is clean.
    assert not (git_repo / "sneaky.py").exists()
    assert _git(git_repo, "status", "--porcelain").stdout.strip() == ""


def test_guarded_in_scope_change_succeeds(git_repo):
    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _create_output(run_id, "allowed.py"),
        run_id,
        files_expected=["allowed.py"],
    )
    assert outcome.success is True
    assert (git_repo / "allowed.py").exists()


# --- rollback failure -> manual intervention ------------------------------ #


def test_guarded_rollback_failure_flags_manual_intervention(git_repo, monkeypatch):
    import backend.pipeline.patch_applier as patch_applier

    # Simulate a rollback that claims success but leaves the tree dirty.
    monkeypatch.setattr(patch_applier, "rollback_patch", lambda *a, **k: True)

    run_id = str(uuid.uuid4())
    outcome = apply_patch_guarded(
        _create_output(run_id, "sneaky.py"),
        run_id,
        files_expected=["allowed.py"],
    )
    assert outcome.failure.failure_type == PatchFailureType.SCOPE_VIOLATION
    assert outcome.failure.rollback_performed is True
    assert outcome.failure.working_tree_clean is False
    assert outcome.failure.manual_intervention_needed is True


# --- helper unit tests ---------------------------------------------------- #


@pytest.mark.parametrize(
    "message, expected",
    [
        ("patch_applier.py: [SECURITY] forbidden path rejected: .env", PatchFailureType.FORBIDDEN_FILE),
        ("edit target missing: x.py", PatchFailureType.TARGET_MISSING),
        ("create target already exists: x.py", PatchFailureType.PATCH_DOES_NOT_APPLY),
        ("edit old_string not found in x.py", PatchFailureType.PATCH_DOES_NOT_APPLY),
        ("edit old_string is not unique in x.py", PatchFailureType.PATCH_DOES_NOT_APPLY),
        ("Large files cannot be replaced wholesale automatically", PatchFailureType.PATCH_MALFORMED),
        ("invalid action 'frobnicate' for x.py", PatchFailureType.PATCH_MALFORMED),
        ("something totally unexpected", PatchFailureType.UNKNOWN_PATCH_FAILURE),
    ],
)
def test_classify_patch_failure_mapping(message, expected):
    assert classify_patch_failure(RuntimeError(message), phase="apply") == expected


def test_classify_test_phase_is_test_failure():
    assert (
        classify_patch_failure(RuntimeError("boom"), phase="test")
        == PatchFailureType.TEST_FAILURE_AFTER_APPLY
    )


def test_validate_changed_files_in_scope_rules():
    assert validate_changed_files_in_scope(["a.py"], ["a.py", "b.py"]) is True
    assert validate_changed_files_in_scope(["c.py"], ["a.py", "b.py"]) is False
    assert validate_changed_files_in_scope([], ["a.py"]) is True
    # Empty allowed scope is unsafe.
    assert validate_changed_files_in_scope(["a.py"], []) is False
    # Windows separators normalize to forward slashes for comparison.
    assert validate_changed_files_in_scope(["src\\a.py"], ["src/a.py"]) is True


def test_actual_changed_files_reports_changes_in_git_repo(git_repo):
    (git_repo / "obs.py").write_text("x = 1\n", encoding="utf-8")
    observed = actual_changed_files(str(git_repo))
    assert "obs.py" in observed
