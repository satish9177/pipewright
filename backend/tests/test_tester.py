"""
test_tester.py
Tests for tester.py pipeline stage.
No API calls. No Gemini.
Uses real subprocess commands available on Windows.
Mocks patch_result with pre-built PatchResult objects.
"""

import uuid
import pytest
from backend.models.handoff import PatchResult, PipelineTestResult
from backend.pipeline.tester import run_tests

pytestmark = pytest.mark.unit


def make_patch_result(run_id: str) -> PatchResult:
    return PatchResult(
        run_id=run_id,
        success=True,
        diff="--- a/test.py\n+++ b/test.py\n",
        pre_patch_git_hash="abc123",
        post_patch_git_hash="def456",
        files_applied=["test.py"],
        rollback_available=True
    )


def test_empty_or_failed_patch_skips_subprocess_and_checkpoint(monkeypatch):
    import backend.pipeline.tester as tester

    run_id = str(uuid.uuid4())
    patch = PatchResult(
        run_id=run_id,
        success=False,
        diff="",
        pre_patch_git_hash="abc123",
        post_patch_git_hash="abc123",
        files_applied=[],
        rollback_available=False,
    )
    monkeypatch.setattr(
        tester.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess should not run")
        ),
    )
    monkeypatch.setattr(
        tester,
        "save_checkpoint",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpoint should not be saved")
        ),
    )

    result = run_tests(patch, run_id)

    assert result.passed is False
    assert result.output == "No patch was applied; tests skipped."
    assert result.total_tests == 0
    assert result.passed_tests == 0
    assert result.failed_tests == 0


def test_passing_command_returns_passed(monkeypatch, tmp_path):
    """
    Use 'python --version' as test command.
    Always exits 0. Should return passed=True.
    """
    from backend.config import keys
    monkeypatch.setattr(
        keys.settings,
        "test_command",
        "python --version"
    )
    monkeypatch.setattr(
        keys.settings,
        "target_repo_path",
        str(tmp_path)
    )

    run_id = str(uuid.uuid4())
    patch = make_patch_result(run_id)

    result = run_tests(patch, run_id)

    assert isinstance(result, PipelineTestResult)
    assert result.passed is True
    assert result.run_id == run_id
    assert result.output is not None


def test_failing_command_returns_failed(monkeypatch, tmp_path):
    """
    Use a command that always fails.
    Should return passed=False and trigger rollback attempt.
    """
    from backend.config import keys
    monkeypatch.setattr(
        keys.settings,
        "test_command",
        "python -c \"import sys; sys.exit(1)\""
    )
    monkeypatch.setattr(
        keys.settings,
        "target_repo_path",
        str(tmp_path)
    )

    run_id = str(uuid.uuid4())
    patch = make_patch_result(run_id)

    result = run_tests(patch, run_id)

    assert isinstance(result, PipelineTestResult)
    assert result.passed is False
    assert result.run_id == run_id


def test_result_contains_output(monkeypatch, tmp_path):
    """
    Output field must always contain something.
    """
    from backend.config import keys
    monkeypatch.setattr(
        keys.settings,
        "test_command",
        "python --version"
    )
    monkeypatch.setattr(
        keys.settings,
        "target_repo_path",
        str(tmp_path)
    )

    run_id = str(uuid.uuid4())
    patch = make_patch_result(run_id)

    result = run_tests(patch, run_id)

    assert result.output is not None
    assert len(result.output) > 0


def test_duration_is_recorded(monkeypatch, tmp_path):
    """
    duration_seconds must be greater than zero.
    """
    from backend.config import keys
    monkeypatch.setattr(
        keys.settings,
        "test_command",
        "python --version"
    )
    monkeypatch.setattr(
        keys.settings,
        "target_repo_path",
        str(tmp_path)
    )

    run_id = str(uuid.uuid4())
    patch = make_patch_result(run_id)

    result = run_tests(patch, run_id)

    assert result.duration_seconds > 0


def test_failing_command_rolls_back_chunk(monkeypatch, tmp_path):
    from backend.config import keys
    import backend.pipeline.tester as tester

    monkeypatch.setattr(
        keys.settings,
        "test_command",
        "python -c \"import sys; sys.exit(1)\""
    )
    monkeypatch.setattr(
        keys.settings,
        "target_repo_path",
        str(tmp_path)
    )

    calls = []

    def fake_rollback(run_id: str, chunk_number: int = 0) -> bool:
        calls.append((run_id, chunk_number))
        return True

    monkeypatch.setattr(tester, "rollback_patch", fake_rollback)

    run_id = str(uuid.uuid4())
    patch = make_patch_result(run_id)
    result = run_tests(patch, run_id, chunk_number=2)

    assert result.passed is False
    assert calls == [(run_id, 2)]
