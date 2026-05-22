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
from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline.patch_applier import apply_patch, rollback_patch

pytestmark = pytest.mark.unit

# Use local folder instead of Windows system temp
LOCAL_TMP = Path(__file__).parent.parent.parent / ".pytest_tmp"


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