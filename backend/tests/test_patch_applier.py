"""
test_patch_applier.py
Tests for patch_applier.py
No API calls. Pure file operations.
Uses local .pytest_tmp folder instead of
Windows system temp (which denies access).
"""

import uuid
import shutil
import pytest
from pathlib import Path
from pydantic import ValidationError
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline.patch_applier import BACKUP_DIR, apply_patch, rollback_patch

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
