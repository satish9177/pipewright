"""
test_tester.py
Tests for tester.py pipeline stage.
No API calls. No Gemini.
Uses real subprocess commands available on Windows.
Mocks patch_result with pre-built PatchResult objects.
"""

import os
import subprocess
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


def test_baseline_only_failures_pass_without_rollback(monkeypatch, tmp_path):
    from backend.config import keys
    import backend.pipeline.tester as tester

    _set_command(monkeypatch, tmp_path, "pytest")

    old_id = "tests/test_app.py::test_old"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=f"FAILED {old_id} - AssertionError\n1 failed\n",
            stderr="",
        )

    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(
        tester,
        "rollback_patch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("baseline-only failures should not roll back")
        ),
    )
    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_path))

    run_id = str(uuid.uuid4())
    result = run_tests(
        make_patch_result(run_id),
        run_id,
        baseline_failing_test_ids=[old_id],
    )

    assert result.passed is True
    assert result.baseline_accepted is True
    assert result.pre_existing_failing_test_ids == [old_id]
    assert result.newly_failing_test_ids == []
    assert result.exit_code == 1


def test_new_failure_vs_baseline_fails_and_rolls_back(monkeypatch, tmp_path):
    import backend.pipeline.tester as tester

    _set_command(monkeypatch, tmp_path, "pytest")

    old_id = "tests/test_app.py::test_old"
    new_id = "tests/test_app.py::test_new"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=(
                f"FAILED {old_id} - AssertionError\n"
                f"FAILED {new_id} - AssertionError\n"
                "2 failed\n"
            ),
            stderr="",
        )

    calls = []
    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(
        tester,
        "rollback_patch",
        lambda run_id, chunk_number=0: calls.append((run_id, chunk_number)) or True,
    )

    run_id = str(uuid.uuid4())
    result = run_tests(
        make_patch_result(run_id),
        run_id,
        chunk_number=3,
        baseline_failing_test_ids=[old_id],
    )

    assert result.passed is False
    assert result.pre_existing_failing_test_ids == [old_id]
    assert result.newly_failing_test_ids == [new_id]
    assert calls == [(run_id, 3)]


def test_unparseable_failing_ids_fail_safe_and_roll_back(monkeypatch, tmp_path):
    import backend.pipeline.tester as tester

    _set_command(monkeypatch, tmp_path, "pytest")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="1 failed, 4 passed in 0.30s\n",
            stderr="",
        )

    calls = []
    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(
        tester,
        "rollback_patch",
        lambda run_id, chunk_number=0: calls.append((run_id, chunk_number)) or True,
    )

    run_id = str(uuid.uuid4())
    result = run_tests(
        make_patch_result(run_id),
        run_id,
        baseline_failing_test_ids=["tests/test_app.py::test_old"],
    )

    assert result.passed is False
    assert result.failing_test_ids_parseable is False
    assert "no node IDs" in result.failing_test_ids_parse_error
    assert calls == [(run_id, 0)]


def test_timeout_is_not_accepted_by_baseline(monkeypatch, tmp_path):
    import backend.pipeline.tester as tester

    _set_command(monkeypatch, tmp_path, "pytest")

    def fake_run(*args, **kwargs):
        error = subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout"],
            output="FAILED tests/test_app.py::test_old - AssertionError\n1 failed",
            stderr="",
        )
        error.stdout = "FAILED tests/test_app.py::test_old - AssertionError\n1 failed"
        raise error

    calls = []
    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(
        tester,
        "rollback_patch",
        lambda run_id, chunk_number=0: calls.append((run_id, chunk_number)) or True,
    )

    run_id = str(uuid.uuid4())
    result = run_tests(
        make_patch_result(run_id),
        run_id,
        baseline_failing_test_ids=["tests/test_app.py::test_old"],
    )

    assert result.passed is False
    assert result.timed_out is True
    assert result.failing_test_ids_parseable is False
    assert calls == [(run_id, 0)]


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


# --------------------------------------------------------------------------
# E2 — stdin=DEVNULL: the parent's stdin must never leak into the test
# subprocess. The anchor pins the subprocess.run call contract; the probe
# tests prove EOF semantics and inheritance override with real subprocesses.
# Probes are written into the fake repo (tmp_path) and referenced by BARE
# FILENAME: run_tests executes with cwd=target_repo_path, so the relative
# name resolves there and no absolute path ever needs quoting under
# shell=True on Windows.
# --------------------------------------------------------------------------


def _write_probe(tmp_path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


def test_subprocess_run_called_with_stdin_devnull_and_contract_unchanged(
    monkeypatch, tmp_path
):
    """
    Anchor: run_tests must call subprocess.run with stdin=subprocess.DEVNULL
    and leave the rest of the call contract exactly as it was. This is the
    red/green pin for E2 — it fails on the pre-fix call in every environment,
    independent of how the test harness's own stdin is wired.
    """
    import subprocess
    import backend.pipeline.tester as tester

    _set_command(monkeypatch, tmp_path, "echo anchor")

    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="1 passed in 0.01s", stderr=""
        )

    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(tester, "save_checkpoint", lambda **kwargs: {"id": "x"})

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    # The new kwarg: stdin is redirected to the null device.
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    # Everything else about the call is unchanged by E2.
    assert captured["args"] == ("echo anchor",)
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["shell"] is True
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["timeout"] == tester.TESTER_TIMEOUT_SECONDS
    assert set(captured["kwargs"]) == {
        "cwd", "shell", "capture_output", "text", "timeout", "stdin"
    }


def test_devnull_gives_stdin_reading_command_immediate_eof(
    monkeypatch, tmp_path
):
    """
    EOF semantics, proven with a real subprocess: a test command that reads
    stdin and exits 0 only on empty input must be classified passed, because
    DEVNULL makes the read return '' immediately.
    """
    _write_probe(
        tmp_path,
        "read_stdin_probe.py",
        "import sys\n"
        "data = sys.stdin.read()\n"
        "print('1 passed' if data == '' else '1 failed')\n"
        "sys.exit(0 if data == '' else 1)\n",
    )
    _set_command(monkeypatch, tmp_path, "python read_stdin_probe.py")

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    assert result.passed_tests == 1


def test_unbounded_stdin_read_completes_well_under_timeout(
    monkeypatch, tmp_path
):
    """
    The extreme case — the production hang: an unbounded sys.stdin.read()
    blocks to the 300s TESTER_TIMEOUT_SECONDS if the child inherits an open
    stdin. With DEVNULL it returns at once. The 60s bound is generous
    (interpreter startup is ~1s) so slow CI cannot flake it, while the
    pre-fix hang path returns passed=False after ~300s and fails every
    assert here.
    """
    _write_probe(
        tmp_path,
        "drain_stdin_probe.py",
        "import sys\n"
        "sys.stdin.read()\n"
        "print('1 passed in 0.01s')\n",
    )
    _set_command(monkeypatch, tmp_path, "python drain_stdin_probe.py")

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is True
    assert result.duration_seconds < 60
    assert result.passed_tests == 1


def test_child_stdin_is_null_device_even_when_parent_stdin_is_a_pipe(
    monkeypatch, tmp_path
):
    """
    Adversarial: wire the parent's fd 0 to a held-open pipe — the exact
    inherited-stdin hang condition — and prove the child still sees the null
    character device, i.e. DEVNULL overrides inheritance. The probe inspects
    its stdin's device type instead of reading from it, so a wrong answer is
    a fast exit(1), never a 300s block.
    """
    _write_probe(
        tmp_path,
        "stdin_device_probe.py",
        "import os, stat, sys\n"
        "is_chr = stat.S_ISCHR(os.fstat(0).st_mode)\n"
        "print('1 passed' if is_chr else '1 failed')\n"
        "sys.exit(0 if is_chr else 1)\n",
    )
    _set_command(monkeypatch, tmp_path, "python stdin_device_probe.py")

    try:
        saved_stdin_fd = os.dup(0)
    except OSError:
        pytest.skip("fd 0 is not duplicable in this environment")

    read_end, write_end = os.pipe()
    os.dup2(read_end, 0)
    try:
        run_id = str(uuid.uuid4())
        result = run_tests(make_patch_result(run_id), run_id)
    finally:
        os.dup2(saved_stdin_fd, 0)
        os.close(saved_stdin_fd)
        os.close(read_end)
        os.close(write_end)

    assert result.passed is True
    assert result.passed_tests == 1


def test_devnull_does_not_mask_real_test_failures(monkeypatch, tmp_path):
    """
    Regression guard: a genuinely failing test command is still classified
    failed after E2 — DEVNULL must not make the world look green. (Rollback
    wiring on this path is already pinned by
    test_failing_command_rolls_back_chunk.)
    """
    _write_probe(
        tmp_path,
        "always_fail_probe.py",
        "import sys\n"
        "print('1 failed')\n"
        "sys.exit(1)\n",
    )
    _set_command(monkeypatch, tmp_path, "python always_fail_probe.py")

    run_id = str(uuid.uuid4())
    result = run_tests(make_patch_result(run_id), run_id)

    assert result.passed is False
    assert result.failed_tests == 1
