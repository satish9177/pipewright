"""
tester.py
Runs the configured test command against the target repo.
Triggers rollback through patch_applier if tests fail.
"""

import re
import sys
import time
import subprocess
from backend.models.handoff import PatchResult, TestResult
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.pipeline.patch_applier import rollback_patch
from backend.projects.project_context import get_target_repo_path, get_test_command

TESTER_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 10000


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


def _combine_output(stdout: str, stderr: str) -> str:
    try:
        output = f"{stdout or ''}{stderr or ''}"
        if not output.strip():
            output = "[TESTER] No test output captured"

        if len(output) > MAX_OUTPUT_CHARS:
            return (
                output[:MAX_OUTPUT_CHARS]
                + f"\n[TESTER] Output truncated to {MAX_OUTPUT_CHARS} chars"
            )
        return output
    except Exception as error:
        raise RuntimeError(f"tester.py: failed to combine output: {error}")


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
    run_id: str
) -> TestResult:
    """
    Synchronous. No AI calls. Pure subprocess.
    Runs test command, captures output,
    triggers rollback on failure.
    """
    print(f"[TESTER] Starting | run_id={run_id}")

    start = time.perf_counter()
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
            timeout=TESTER_TIMEOUT_SECONDS
        )
        duration = time.perf_counter() - start
        output = _combine_output(completed.stdout, completed.stderr)
        total_tests, passed_tests, failed_tests = _parse_test_counts(output)
        passed = completed.returncode == 0

        print(f"[TESTER] Duration: {duration:.2f} seconds")

        if passed:
            _warn_if_failure_strings(output)
            print(
                f"[TESTER] Result: PASSED | "
                f"{passed_tests} passed {failed_tests} failed"
            )

            test_result = TestResult(
                run_id=run_id,
                passed=True,
                total_tests=total_tests,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                output=output,
                duration_seconds=duration
            )

            save_checkpoint(
                run_id=run_id,
                step="test",
                output=test_result.model_dump(),
                handoff_contract=test_result.model_dump(),
                git_hash=patch_result.post_patch_git_hash,
                tests_passed=True
            )
            print(f"[TESTER] Checkpoint saved | run_id={run_id}")
            print(f"[TESTER] Complete | run_id={run_id}")
            return test_result

        print(f"[TESTER] Result: FAILED | {failed_tests} failed")
        print("[TESTER] Tests failed. Triggering rollback.")
        rollback_result = rollback_patch(run_id)
        if rollback_result:
            print("[TESTER] Rollback complete.")
        else:
            print("[TESTER] Rollback not available.")

        print(f"[TESTER] Complete | run_id={run_id}")
        return TestResult(
            run_id=run_id,
            passed=False,
            total_tests=total_tests,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            output=output,
            duration_seconds=duration
        )

    except subprocess.TimeoutExpired as error:
        duration = time.perf_counter() - start
        output = _combine_output(error.stdout or "", error.stderr or "")
        print(f"[TESTER] Duration: {duration:.2f} seconds")
        print("[TESTER] Result: FAILED | command timed out")
        print("[TESTER] Tests failed. Triggering rollback.")
        rollback_result = rollback_patch(run_id)
        if rollback_result:
            print("[TESTER] Rollback complete.")
        else:
            print("[TESTER] Rollback not available.")

        return TestResult(
            run_id=run_id,
            passed=False,
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            output=output,
            duration_seconds=duration
        )
    except Exception as error:
        duration = time.perf_counter() - start
        print(f"[TESTER] Duration: {duration:.2f} seconds")
        print("[TESTER] Result: FAILED | unexpected error")
        print("[TESTER] Tests failed. Triggering rollback.")
        try:
            rollback_result = rollback_patch(run_id)
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
