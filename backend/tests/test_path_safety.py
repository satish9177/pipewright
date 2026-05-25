"""
Tests for shared target repo path safety.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline import coder, patch_applier
from backend.utils.path_safety import (
    is_forbidden_path,
    normalize_relative_path,
    validate_safe_relative_path,
)

pytestmark = pytest.mark.unit


def _handoff(path: str, content: str = "print('ok')\n") -> CoderHandoff:
    return CoderHandoff(
        run_id=str(uuid.uuid4()),
        feature_description="Path safety",
        files_changed=[
            FileChange(
                path=path,
                action="create",
                content=content,
                reason="test",
            )
        ],
        summary="Path safety test",
    )


def test_coder_refuses_to_read_env(tmp_repo):
    (tmp_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forbidden path"):
        coder._read_target_file(".env", str(tmp_repo), coder.MAX_FILE_LINES)


def test_patch_applier_refuses_to_write_env(tmp_repo, monkeypatch):
    monkeypatch.setattr(patch_applier, "get_target_repo_path", lambda: str(tmp_repo))

    with pytest.raises(RuntimeError, match="forbidden path"):
        patch_applier.apply_patch(_handoff(".env"), str(uuid.uuid4()))

    assert not (tmp_repo / ".env").exists()


def test_path_traversal_is_rejected(tmp_repo):
    with pytest.raises(RuntimeError, match="path traversal"):
        validate_safe_relative_path("../../.env", tmp_repo)


def test_windows_separators_are_normalized_safely(tmp_repo):
    normalized = normalize_relative_path("src\\app.py")
    full_path = validate_safe_relative_path("src\\app.py", tmp_repo)

    assert normalized == "src/app.py"
    assert full_path == tmp_repo.resolve() / "src" / "app.py"


def test_coder_refuses_large_file_for_modify(tmp_repo):
    large_file = tmp_repo / "src" / "large.py"
    large_file.parent.mkdir(parents=True)
    large_file.write_text("\n".join("line" for _ in range(coder.MAX_FILE_LINES + 1)), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to modify large file"):
        coder._read_target_file(
            "src/large.py",
            str(tmp_repo),
            coder.MAX_FILE_LINES,
            "modify",
        )


def test_coder_refuses_large_file_for_read_with_read_message(tmp_repo):
    large_file = tmp_repo / "src" / "large_readme.md"
    large_file.parent.mkdir(parents=True)
    large_file.write_text("\n".join("line" for _ in range(coder.MAX_FILE_LINES + 1)), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to read large file"):
        coder._read_target_file(
            "src/large_readme.md",
            str(tmp_repo),
            coder.MAX_FILE_LINES,
            "read",
        )


def test_env_example_and_sample_are_allowed(tmp_repo):
    assert is_forbidden_path(".env") is True
    assert is_forbidden_path(".env.production") is True
    assert is_forbidden_path(".env.example") is False
    assert is_forbidden_path(".env.sample") is False
    assert validate_safe_relative_path(".env.example", tmp_repo) == (
        tmp_repo.resolve() / ".env.example"
    )


def test_patch_applier_safe_file_still_works(tmp_repo, monkeypatch):
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(patch_applier, "get_target_repo_path", lambda: str(tmp_repo))

    result = patch_applier.apply_patch(_handoff("src/app.py"), run_id)

    assert result.success is True
    assert (tmp_repo / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT tests_passed, step_completed FROM checkpoints
            WHERE run_id = :run_id AND step = 'patch'
        """), {"run_id": run_id}).fetchone()
    assert row[0] == 0
    assert row[1] == 1
