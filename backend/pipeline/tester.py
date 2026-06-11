"""
tester.py
Runs the configured test command against the target repo.
Triggers rollback through patch_applier if tests fail.
"""

import re
import sys
import time
import subprocess
from collections.abc import Iterable
from backend.models.handoff import PatchResult, PipelineTestResult
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.pipeline.patch_applier import rollback_patch
from backend.pipeline.policy import MAX_OUTPUT_CHARS, TESTER_TIMEOUT_SECONDS
from backend.pipeline.test_failure_ids import (
    FailingTestIdParseResult,
    extract_pytest_failing_test_ids,
)
from backend.pipeline.test_run_validation import (
    ExecutionIntegrity,
    classify_execution_integrity,
)
from backend.projects.project_context import get_target_repo_path, get_test_command


def _resolve_command(command: str) -> str:
    """
    Resolve python commands to the current interpreter when python is not on PATH.
    Keeps configured command semantics while making Windows unit tests portable.
    """
    try:
        stripped = command.strip()
        if stripped == "python":
            return f'"{sys.executable}"'
        if stripped.startswith("python "):
            return f'"{sys.executable}" {stripped[len("python "):]}'
        return command
    except Exception as error:
        raise RuntimeError(f"tester.py: failed to resolve command: {error}")


def _combine_full_output(stdout: str, stderr: str) -> str:
    """
    Combine stdout and stderr into one UNTRUNCATED string.

    This is the text that evidence parsing (``_parse_test_counts`` here, and the
    runtime classifier wired in later #28 slices) must see. A test summary line
    ("12 passed", "collected 0 items") lives at the END of the output, so parsing
    must run against the full text BEFORE any display truncation. Never raises.
    """
    try:
        output = f"{stdout or ''}{stderr or ''}"
        if not output.strip():
            return "[TESTER] No test output captured"
        return output
    except Exception as error:
        raise RuntimeError(f"tester.py: failed to combine output: {error}")


def _truncate_for_display(output: str) -> str:
    """
    Bound stored/displayed output to MAX_OUTPUT_CHARS while PRESERVING THE TAIL.

    The previous implementation kept the head and dropped everything past
    MAX_OUTPUT_CHARS, which discarded the pytest/jest/go summary line that sits at
    the end — making a healthy large suite look like "0 tests" to any count-based
    logic. We keep the tail instead, prefixed with a clear truncation marker.

    This is a DISPLAY/storage transform only. Evidence must be parsed from the
    full output (see ``_combine_full_output``), never from this string. Never
    raises.
    """
    try:
        if len(output) <= MAX_OUTPUT_CHARS:
            return output
        tail = output[-MAX_OUTPUT_CHARS:]
        return (
            f"[TESTER] ... output truncated ... (showing last "
            f"{MAX_OUTPUT_CHARS} chars)\n"
            + tail
        )
    except Exception as error:
        raise RuntimeError(f"tester.py: failed to truncate output: {error}")


def _parse_test_counts(output: str) -> tuple[int, int, int]:
    """
    Best-effort parser for common pytest counts.
    Never raises to callers.
    """
    try:
        passed_tests = 0
        failed_tests = 0

        passed_match = re.search(r"(\d+)\s+passed", output)
        failed_match = re.search(r"(\d+)\s+failed", output)

        if passed_match:
            passed_tests = int(passed_match.group(1))
        if failed_match:
            failed_tests = int(failed_match.group(1))

        total_tests = passed_tests + failed_tests
        return total_tests, passed_tests, failed_tests
    except Exception as error:
        print(f"[TESTER] Warning: failed to parse test counts: {error}")
        return 0, 0, 0


def _parse_failing_test_ids_for_result(
    output: str,
    *,
    exit_code: int | None,
    timed_out: bool,
) -> FailingTestIdParseResult:
    parsed = extract_pytest_failing_test_ids(output)
    if timed_out:
        return FailingTestIdParseResult(
            failed_ids=parsed.failed_ids,
            parseable=False,
            reason="test command timed out before failing IDs could be trusted",
            expected_failed_count=parsed.expected_failed_count,
        )
    if exit_code not in (None, 0) and parsed.parseable and not parsed.failed_ids:
        return FailingTestIdParseResult(
            failed_ids=frozenset(),
            parseable=False,
            reason=(
                "test command exited non-zero without parseable pytest failing "
                "node IDs"
            ),
            expected_failed_count=parsed.expected_failed_count,
        )
    return parsed


def _normalize_id_set(values: Iterable[str] | None) -> set[str]:
    if values is None:
        return set()
    return {str(value) for value in values if str(value).strip()}


def _baseline_delta(
    current_ids: Iterable[str],
    baseline_ids: set[str],
) -> tuple[list[str], list[str]]:
    current = set(current_ids)
    return sorted(current & baseline_ids), sorted(current - baseline_ids)


def _baseline_accepts_failed_result(
    *,
    exit_code: int | None,
    timed_out: bool,
    output: str,
    parsed_ids: FailingTestIdParseResult,
    baseline_ids: set[str],
    baseline_ids_parseable: bool,
) -> bool:
    if not baseline_ids_parseable or not parsed_ids.parseable:
        return False
    integrity = classify_execution_integrity(
        exit_code,
        output,
        timed_out=timed_out,
    )
    if integrity is not ExecutionIntegrity.OK:
        return False
    _pre_existing, newly_failing = _baseline_delta(parsed_ids.failed_ids, baseline_ids)
    return not newly_failing


def _make_test_result(
    *,
    run_id: str,
    passed: bool,
    exit_code: int | None,
    timed_out: bool = False,
    total_tests: int = 0,
    passed_tests: int = 0,
    failed_tests: int = 0,
    output: str,
    duration_seconds: float,
    parsed_ids: FailingTestIdParseResult | None = None,
    baseline_ids: set[str] | None = None,
    baseline_accepted: bool = False,
) -> PipelineTestResult:
    parsed_ids = parsed_ids or FailingTestIdParseResult(
        failed_ids=frozenset(),
        parseable=True,
        reason="no pytest failure evidence",
    )
    baseline_ids = baseline_ids or set()
    pre_existing, newly_failing = _baseline_delta(
        parsed_ids.failed_ids,
        baseline_ids,
    )
    return PipelineTestResult(
        run_id=run_id,
        passed=passed,
        exit_code=exit_code,
        timed_out=timed_out,
        total_tests=total_tests,
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        failing_test_ids=sorted(parsed_ids.failed_ids),
        failing_test_ids_parseable=parsed_ids.parseable,
        failing_test_ids_parse_error=None if parsed_ids.parseable else parsed_ids.reason,
        baseline_accepted=baseline_accepted,
        baseline_failing_test_ids=sorted(baseline_ids),
        pre_existing_failing_test_ids=pre_existing,
        newly_failing_test_ids=newly_failing,
        output=output,
        duration_seconds=duration_seconds,
    )


def _warn_if_failure_strings(output: str) -> None:
    try:
        failure_strings = ["FAILED", "ERROR", "failed", "error"]
        if any(value in output for value in failure_strings):
            print(
                "[TESTER] Warning: output contains failure text, "
                "but returncode is 0"
            )
    except Exception as error:
        print(f"[TESTER] Warning: output scan failed: {error}")


def run_tests(
    patch_result: PatchResult,
    run_id: str,
    chunk_number: int = 0,
    *,
    baseline_failing_test_ids: Iterable[str] | None = None,
    baseline_failing_test_ids_parseable: bool = True,
) -> PipelineTestResult:
    """
    Synchronous. No AI calls. Pure subprocess.
    Runs test command, captures output,
    triggers rollback on failure.
    """
    print(f"[TESTER] Starting | run_id={run_id}")

    start = time.perf_counter()
    if not patch_result.success or not patch_result.files_applied:
        print("[TESTER] No patch was applied; tests skipped.")
        return _make_test_result(
            run_id=run_id,
            passed=False,
            exit_code=None,
            timed_out=False,
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            output="No patch was applied; tests skipped.",
            duration_seconds=time.perf_counter() - start,
            parsed_ids=FailingTestIdParseResult(
                failed_ids=frozenset(),
                parseable=False,
                reason="tests skipped because no patch was applied",
            ),
        )

    command = get_test_command()
    resolved_command = _resolve_command(command)
    target_repo_path = get_target_repo_path()

    print(f"[TESTER] Command: {command}")
    print(f"[TESTER] Working directory: {target_repo_path}")
    print("[TESTER] Running tests...")

    try:
        completed = subprocess.run(
            resolved_command,
            cwd=target_repo_path,
            shell=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TESTER_TIMEOUT_SECONDS
        )
        duration = time.perf_counter() - start
        # Parse counts from the FULL output (summary line is at the end), then
        # store a tail-preserving truncated copy for display. #28C.
        full_output = _combine_full_output(completed.stdout, completed.stderr)
        total_tests, passed_tests, failed_tests = _parse_test_counts(full_output)
        parsed_ids = _parse_failing_test_ids_for_result(
            full_output,
            exit_code=completed.returncode,
            timed_out=False,
        )
        output = _truncate_for_display(full_output)
        baseline_ids = _normalize_id_set(baseline_failing_test_ids)
        baseline_accepted = False
        if completed.returncode != 0:
            baseline_accepted = _baseline_accepts_failed_result(
                exit_code=completed.returncode,
                timed_out=False,
                output=full_output,
                parsed_ids=parsed_ids,
                baseline_ids=baseline_ids,
                baseline_ids_parseable=baseline_failing_test_ids_parseable,
            )
        passed = completed.returncode == 0 or baseline_accepted

        print(f"[TESTER] Duration: {duration:.2f} seconds")

        if passed:
            if baseline_accepted:
                print("[TESTER] Result: PASSED | no new failures vs baseline")
            else:
                _warn_if_failure_strings(output)
            print(
                f"[TESTER] Result: PASSED | "
                f"{passed_tests} passed {failed_tests} failed"
            )

            test_result = _make_test_result(
                run_id=run_id,
                passed=True,
                exit_code=completed.returncode,
                total_tests=total_tests,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                parsed_ids=parsed_ids,
                baseline_ids=baseline_ids,
                baseline_accepted=baseline_accepted,
                output=output,
                duration_seconds=duration
            )

            save_checkpoint(
                run_id=run_id,
                step="test",
                output=test_result.model_dump(),
                handoff_contract=test_result.model_dump(),
                git_hash=patch_result.post_patch_git_hash,
                tests_passed=True,
                step_completed=True,
                chunk_number=chunk_number
            )
            print(f"[TESTER] Checkpoint saved | run_id={run_id}")
            print(f"[TESTER] Complete | run_id={run_id}")
            return test_result

        print(f"[TESTER] Result: FAILED | {failed_tests} failed")
        print("[TESTER] Tests failed. Triggering rollback.")
        rollback_result = rollback_patch(run_id, chunk_number)
        if rollback_result:
            print("[TESTER] Rollback complete.")
        else:
            print("[TESTER] Rollback not available.")

        print(f"[TESTER] Complete | run_id={run_id}")
        return _make_test_result(
            run_id=run_id,
            passed=False,
            exit_code=completed.returncode,
            total_tests=total_tests,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            parsed_ids=parsed_ids,
            baseline_ids=baseline_ids,
            output=output,
            duration_seconds=duration
        )

    except subprocess.TimeoutExpired as error:
        duration = time.perf_counter() - start
        full_output = _combine_full_output(error.stdout or "", error.stderr or "")
        output = _truncate_for_display(full_output)
        parsed_ids = _parse_failing_test_ids_for_result(
            full_output,
            exit_code=None,
            timed_out=True,
        )
        baseline_ids = _normalize_id_set(baseline_failing_test_ids)
        print(f"[TESTER] Duration: {duration:.2f} seconds")
        print("[TESTER] Result: FAILED | command timed out")
        print("[TESTER] Tests failed. Triggering rollback.")
        rollback_result = rollback_patch(run_id, chunk_number)
        if rollback_result:
            print("[TESTER] Rollback complete.")
        else:
            print("[TESTER] Rollback not available.")

        return _make_test_result(
            run_id=run_id,
            passed=False,
            exit_code=None,
            timed_out=True,
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            parsed_ids=parsed_ids,
            baseline_ids=baseline_ids,
            output=output,
            duration_seconds=duration
        )
    except Exception as error:
        duration = time.perf_counter() - start
        print(f"[TESTER] Duration: {duration:.2f} seconds")
        print("[TESTER] Result: FAILED | unexpected error")
        print("[TESTER] Tests failed. Triggering rollback.")
        try:
            rollback_result = rollback_patch(run_id, chunk_number)
            if rollback_result:
                print("[TESTER] Rollback complete.")
            else:
                print("[TESTER] Rollback not available.")
        except Exception as rollback_error:
            raise RuntimeError(
                f"tester.py: test execution failed and rollback failed. "
                f"run_id={run_id} | error={error} | "
                f"rollback_error={rollback_error}"
            )

        raise RuntimeError(
            f"tester.py: test execution failed. run_id={run_id} | error={error}"
        )


def run_baseline_tests(run_id: str) -> PipelineTestResult:
    """
    Run the configured test command before chunk execution.

    Baseline verification has no patch to roll back and saves no chunk checkpoint;
    it only records whether the run starts green or which pytest node IDs are
    already red.
    """
    print(f"[TESTER] Starting baseline | run_id={run_id}")
    start = time.perf_counter()
    command = get_test_command()
    resolved_command = _resolve_command(command)
    target_repo_path = get_target_repo_path()

    print(f"[TESTER] Baseline command: {command}")
    print(f"[TESTER] Baseline working directory: {target_repo_path}")

    try:
        completed = subprocess.run(
            resolved_command,
            cwd=target_repo_path,
            shell=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TESTER_TIMEOUT_SECONDS,
        )
        duration = time.perf_counter() - start
        full_output = _combine_full_output(completed.stdout, completed.stderr)
        total_tests, passed_tests, failed_tests = _parse_test_counts(full_output)
        parsed_ids = _parse_failing_test_ids_for_result(
            full_output,
            exit_code=completed.returncode,
            timed_out=False,
        )
        output = _truncate_for_display(full_output)
        passed = completed.returncode == 0

        print(f"[TESTER] Baseline duration: {duration:.2f} seconds")
        print(
            f"[TESTER] Baseline result: {'PASSED' if passed else 'FAILED'} | "
            f"{passed_tests} passed {failed_tests} failed"
        )
        return _make_test_result(
            run_id=run_id,
            passed=passed,
            exit_code=completed.returncode,
            total_tests=total_tests,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            parsed_ids=parsed_ids,
            output=output,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired as error:
        duration = time.perf_counter() - start
        full_output = _combine_full_output(error.stdout or "", error.stderr or "")
        parsed_ids = _parse_failing_test_ids_for_result(
            full_output,
            exit_code=None,
            timed_out=True,
        )
        output = _truncate_for_display(full_output)
        print(f"[TESTER] Baseline duration: {duration:.2f} seconds")
        print("[TESTER] Baseline result: FAILED | command timed out")
        return _make_test_result(
            run_id=run_id,
            passed=False,
            exit_code=None,
            timed_out=True,
            parsed_ids=parsed_ids,
            output=output,
            duration_seconds=duration,
        )
    except Exception as error:
        duration = time.perf_counter() - start
        print(f"[TESTER] Baseline duration: {duration:.2f} seconds")
        raise RuntimeError(
            f"tester.py: baseline test execution failed. "
            f"run_id={run_id} | error={error}"
        )
