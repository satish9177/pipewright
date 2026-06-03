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
from backend.pipeline.tester import (
    MAX_OUTPUT_CHARS,
    run_tests,
    _combine_full_output,
    _parse_test_counts,
    _truncate_for_display,
)

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


# --------------------------------------------------------------------------
# #28C — output combination / truncation helpers (pure, no subprocess)
# --------------------------------------------------------------------------


def test_combine_full_output_keeps_both_streams():
    combined = _combine_full_output("stdout-part ", "stderr-part")
    assert "stdout-part" in combined
    assert "stderr-part" in combined


def test_combine_full_output_does_not_truncate_long_output():
    # The full-output helper must never truncate; parsing depends on the tail.
    big = "x" * (MAX_OUTPUT_CHARS * 2) + "12 passed in 1.23s"
    combined = _combine_full_output(big, "")
    assert combined == big
    assert "12 passed in 1.23s" in combined


def test_combine_full_output_empty_has_placeholder():
    assert _combine_full_output("", "") == "[TESTER] No test output captured"
    assert _combine_full_output("   ", "") == "[TESTER] No test output captured"


def test_truncate_for_display_short_output_unchanged():
    short = "5 passed in 0.10s"
    assert _truncate_for_display(short) == short


def test_truncate_for_display_preserves_tail_and_marks_truncation():
    # Summary lives at the END; truncation must keep the tail, not the head.
    head = "HEAD-MARKER " + ("noise line\n" * 5000)
    output = head + "12 passed in 1.23s"
    assert len(output) > MAX_OUTPUT_CHARS

    truncated = _truncate_for_display(output)

    assert len(truncated) <= MAX_OUTPUT_CHARS + 200  # marker overhead only
    assert "... output truncated ..." in truncated
    assert "12 passed in 1.23s" in truncated  # tail (summary) preserved
    assert "HEAD-MARKER" not in truncated      # head dropped, as intended


def test_truncate_for_display_retains_zero_test_marker_at_tail():
    output = ("noise line\n" * 5000) + "collected 0 items"
    assert len(output) > MAX_OUTPUT_CHARS
    truncated = _truncate_for_display(output)
    assert "collected 0 items" in truncated


def test_parse_counts_sees_summary_at_end_of_long_output():
    # Regression for the front-truncation bug: parsing the FULL output finds the
    # end-of-output summary that head-truncation would have discarded.
    full = ("verbose progress line\n" * 5000) + "12 passed in 1.23s"
    assert len(full) > MAX_OUTPUT_CHARS
    total, passed, failed = _parse_test_counts(full)
    assert passed == 12
    assert failed == 0
    assert total == 12


def test_head_truncation_would_have_lost_summary():
    # Documents *why* #28C exists: keeping the head loses the trailing summary,
    # so parsing the head would wrongly find no counts.
    full = ("verbose progress line\n" * 5000) + "12 passed in 1.23s"
    head_only = full[:MAX_OUTPUT_CHARS]
    assert "12 passed" not in head_only
    assert "12 passed" in _truncate_for_display(full)


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


# --------------------------------------------------------------------------
# #28C — end-to-end: long real-subprocess output with summary at the end
# --------------------------------------------------------------------------


def _set_command(monkeypatch, tmp_path, command: str) -> None:
    from backend.config import keys
    monkeypatch.setattr(keys.settings, "test_command", command)
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_path))


def test_long_output_summary_at_end_is_parsed_and_tail_preserved(
    monkeypatch, tmp_path
):
    # Print >MAX_OUTPUT_CHARS of noise, then a pytest-style summary, exit 0.
    _set_command(
        monkeypatch,
        tmp_path,
        "python -c \"print('x' * 20000); print('12 passed in 1.23s')\"",
    )

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    # Pass/fail stays exit-code based; counts parsed from FULL output.
    assert result.passed is True
    assert result.passed_tests == 12
    assert result.total_tests == 12
    # Stored output is truncated but retains the trailing summary.
    assert len(result.output) <= MAX_OUTPUT_CHARS + 200
    assert "12 passed in 1.23s" in result.output
    assert "... output truncated ..." in result.output


def test_long_output_zero_test_marker_at_end_preserved(monkeypatch, tmp_path):
    # A real-but-empty pytest run: exit 0, "collected 0 items" at the end.
    # This slice must NOT change pass/fail based on the zero-test marker.
    _set_command(
        monkeypatch,
        tmp_path,
        "python -c \"print('x' * 20000); print('collected 0 items')\"",
    )

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True  # exit-code based, unchanged by #28C
    assert result.total_tests == 0
    assert "collected 0 items" in result.output


def test_stderr_only_output_is_captured(monkeypatch, tmp_path):
    _set_command(
        monkeypatch,
        tmp_path,
        "python -c \"import sys; sys.stderr.write('stderr-only-evidence')\"",
    )

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    assert "stderr-only-evidence" in result.output
