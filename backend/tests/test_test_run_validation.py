"""
test_test_run_validation.py
Unit tests for the pure runtime test-validation classifier (#28B).

Pure classification: no execution, no filesystem, no DB, no subprocess. Every
case below is a deterministic function call over (command, exit_code, output).
"""

import pytest

from backend.pipeline.test_run_validation import (
    TestRunVerdict,
    classify_test_run,
)

pytestmark = pytest.mark.unit


def _verdict(command, exit_code, output) -> TestRunVerdict:
    return classify_test_run(command, exit_code, output).verdict


# --------------------------------------------------------------------------
# 1. Blank/missing command -> NONE
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["", "   ", "\t", None])
def test_blank_command_is_none(command):
    assert _verdict(command, 0, "") is TestRunVerdict.NONE


# --------------------------------------------------------------------------
# 2-5. Weak command strings stay WEAK on the success path, regardless of output
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "python --version",
        "node --version",
        "echo ok",
        "pwd",
        "true",
        "npm --version",
    ],
)
def test_weak_command_strings_are_weak(command):
    assert _verdict(command, 0, "") is TestRunVerdict.WEAK


def test_weak_command_stays_weak_even_with_passing_looking_output():
    # Rule 3: a WEAK command short-circuits; output cannot upgrade it.
    assert _verdict("python --version", 0, "5 passed") is TestRunVerdict.WEAK


# --------------------------------------------------------------------------
# 6. pytest exit 0 with "5 passed" -> STRONG
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "output",
    [
        "5 passed",
        "12 passed in 0.42s",
        "2 passed, 1 skipped",
        "===== 5 passed in 0.10s =====",
    ],
)
def test_likely_test_with_passes_is_strong(output):
    result = classify_test_run("pytest", 0, output)
    assert result.verdict is TestRunVerdict.STRONG
    assert result.passed_tests is not None and result.passed_tests >= 1


# --------------------------------------------------------------------------
# 7. Failures present -> never STRONG (nonzero exit, and the contradictory
#    exit-0-with-failures case)
# --------------------------------------------------------------------------

def test_failures_with_nonzero_exit_is_not_strong():
    assert (
        _verdict("pytest", 1, "1 failed, 4 passed")
        is not TestRunVerdict.STRONG
    )


def test_failures_with_success_exit_is_unknown_not_strong():
    # exit 0 + reported failures is contradictory; we document this as a safe
    # non-strong (UNKNOWN) verdict rather than trusting the green exit code.
    result = classify_test_run("pytest", 0, "1 failed, 4 passed in 1.0s")
    assert result.verdict is TestRunVerdict.UNKNOWN
    assert result.verdict is not TestRunVerdict.STRONG
    assert result.passed_tests == 4
    assert result.failed_tests == 1


# --------------------------------------------------------------------------
# 8-9, 11, 17. Zero-test evidence on a recognized runner -> WEAK
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "output",
    [
        "collected 0 items",
        "no tests ran",
        "0 tests collected",
        "collected 0 items / 0 selected",
        "collected 5 items / 5 deselected / 0 selected\nno tests ran in 0.01s",
        "0 passed",
        "0 passed, 5 skipped",
    ],
)
def test_likely_test_with_zero_evidence_is_weak(output):
    result = classify_test_run("pytest", 0, output)
    assert result.verdict is TestRunVerdict.WEAK
    assert result.zero_tests_detected is True


# --------------------------------------------------------------------------
# 10. Recognized runner, unparseable output -> UNKNOWN (absence != zero)
# --------------------------------------------------------------------------

def test_likely_test_with_unparseable_output_is_unknown():
    result = classify_test_run("pytest", 0, "some unrelated output\nnothing useful")
    assert result.verdict is TestRunVerdict.UNKNOWN
    assert result.counts_parsed is False
    assert result.zero_tests_detected is False


def test_missing_summary_is_not_inferred_as_zero():
    result = classify_test_run("pytest", 0, "")
    assert result.zero_tests_detected is False
    assert result.verdict is TestRunVerdict.UNKNOWN


# --------------------------------------------------------------------------
# 12. npm default test stub -> not STRONG (WEAK via "no test specified")
# --------------------------------------------------------------------------

def test_npm_no_test_specified_stub_is_not_strong():
    result = classify_test_run(
        "npm test", 0, 'Error: no test specified'
    )
    assert result.verdict is not TestRunVerdict.STRONG
    assert result.verdict is TestRunVerdict.WEAK


# --------------------------------------------------------------------------
# 13-14. Unknown custom scripts -> UNKNOWN, never WEAK; runtime evidence does
#        NOT upgrade UNKNOWN to STRONG in v1.
# --------------------------------------------------------------------------

def test_unknown_custom_script_unparseable_is_unknown_not_weak():
    result = classify_test_run("./scripts/check-all.sh", 0, "running checks...")
    assert result.verdict is TestRunVerdict.UNKNOWN
    assert result.verdict is not TestRunVerdict.WEAK


def test_unknown_custom_script_with_passing_output_stays_unknown():
    # v1: do not let runtime evidence upgrade an UNKNOWN command to STRONG.
    result = classify_test_run("./scripts/check-all.sh", 0, "5 passed")
    assert result.verdict is TestRunVerdict.UNKNOWN
    assert result.verdict is not TestRunVerdict.STRONG


# --------------------------------------------------------------------------
# 15. exit_code != 0 never returns STRONG
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command,output",
    [
        ("pytest", "5 passed"),
        ("npm test", "5 passed"),
        ("python -m pytest", "12 passed in 0.4s"),
    ],
)
def test_nonzero_exit_never_strong(command, output):
    assert _verdict(command, 1, output) is not TestRunVerdict.STRONG


def test_nonzero_exit_weak_command_stays_weak():
    assert _verdict("python --version", 2, "") is TestRunVerdict.WEAK


def test_nonzero_exit_likely_test_is_unknown():
    assert _verdict("pytest", 1, "5 passed") is TestRunVerdict.UNKNOWN


# --------------------------------------------------------------------------
# 16. Count parsing captures totals when parseable
# --------------------------------------------------------------------------

def test_counts_parsed_fields_populated():
    result = classify_test_run("pytest", 0, "1 failed, 4 passed in 1.0s")
    assert result.counts_parsed is True
    assert result.passed_tests == 4
    assert result.failed_tests == 1
    assert result.total_tests == 5


def test_counts_only_passed():
    result = classify_test_run("pytest", 0, "5 passed in 0.42s")
    assert result.passed_tests == 5
    assert result.failed_tests is None
    assert result.total_tests == 5


# --------------------------------------------------------------------------
# 18. Color / control characters do not break parsing
# --------------------------------------------------------------------------

def test_ansi_colored_output_still_parses_passes():
    colored = "\x1b[32m5 passed\x1b[0m in 0.42s"
    result = classify_test_run("pytest", 0, colored)
    assert result.verdict is TestRunVerdict.STRONG
    assert result.passed_tests == 5


# --------------------------------------------------------------------------
# 19. None inputs handled safely
# --------------------------------------------------------------------------

def test_none_inputs_do_not_raise():
    result = classify_test_run(None, None, None)
    assert result.verdict is TestRunVerdict.NONE


def test_none_output_with_likely_test_is_unknown():
    result = classify_test_run("pytest", 0, None)
    assert result.verdict is TestRunVerdict.UNKNOWN
    assert result.counts_parsed is False


def test_none_exit_code_never_strong():
    # exit_code is None means the success signal is absent; STRONG requires a
    # confirmed exit 0.
    assert _verdict("pytest", None, "5 passed") is not TestRunVerdict.STRONG


# --------------------------------------------------------------------------
# 20. Determinism / purity
# --------------------------------------------------------------------------

def test_classification_is_deterministic():
    args = ("pytest", 0, "5 passed in 0.42s")
    first = classify_test_run(*args)
    for _ in range(5):
        assert classify_test_run(*args) == first


# --------------------------------------------------------------------------
# Regression: the exact #27 manual smoke case must be WEAK.
# --------------------------------------------------------------------------

def test_regression_python_version_smoke_case_is_weak():
    result = classify_test_run("python --version", 0, "Python 3.11.5")
    assert result.verdict is TestRunVerdict.WEAK


# --------------------------------------------------------------------------
# command_quality (Signal A) is surfaced on the result.
# --------------------------------------------------------------------------

def test_result_exposes_command_quality_signal():
    assert classify_test_run("pytest", 0, "5 passed").command_quality == "likely_test"
    assert classify_test_run("python --version", 0, "").command_quality == "weak"
    assert classify_test_run("./x.sh", 0, "").command_quality == "unknown"
