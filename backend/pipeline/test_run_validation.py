"""
test_run_validation.py
Pure deterministic runtime test-validation classifier (#28B).

Why this exists
---------------
Pipewright's safety model has historically treated "the configured test command
exited 0" as "tests passed". A command can exit 0 while validating nothing:
``python --version`` prints a version, ``pytest`` on a repo with no tests
collects 0 items, the default ``npm test`` stub echoes "no test specified". Each
looks green and validated nothing. See docs/design/stronger-test-validation.md.

This module joins two deterministic signals into one verdict:

  - Signal A — command intent: the existing
    ``test_command_quality.classify_test_command`` (WEAK / LIKELY_TEST / UNKNOWN).
    Reused as-is; this module never reimplements or weakens it.
  - Signal B — execution evidence: the test command's exit code plus best-effort
    parsed counts and zero-test markers from its captured output.

Verdict (``TestRunVerdict``):

  - ``STRONG``  — a recognized test runner ran with exit 0 and evidence shows
                  one or more tests actually executed and passed.
  - ``WEAK``    — a recognized weak/no-op/version command, OR a recognized test
                  runner that demonstrably ran 0 tests. Looks green, validated
                  nothing.
  - ``NONE``    — no test command configured.
  - ``UNKNOWN`` — an unrecognized custom command, or a test runner whose output
                  gives no parseable evidence. Pipewright cannot confirm tests
                  ran, and must NOT accuse it of being weak (v1).

This module is pure and deterministic: it reads ONLY the passed-in strings and
exit code. No LLM, no filesystem, no subprocess, no repo inspection, no DB, no
persistence. It only classifies. It never fails a run, never rolls back, never
implies tests passed on a non-zero exit. Wiring into tester.py is #28C/#28D, not
this slice.

Output truncation note (#28C): callers may pass full output or a tail. This
classifier parses whatever it is given; "absence of a parseable count" is treated
as UNKNOWN, never as "zero tests". The tester-side fix to parse before truncation
lands in #28C.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from backend.pipeline.test_command_quality import (
    TestCommandQuality,
    TestCommandQualityResult,
    classify_test_command,
)


class TestRunVerdict(str, Enum):
    # Name begins with "Test", so tell pytest not to collect it as a test class
    # (a dunder, so it is not an enum member).
    __test__ = False

    STRONG = "strong"
    WEAK = "weak"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TestRunValidationResult:
    # Not a pytest test class despite the leading "Test".
    __test__ = False

    verdict: TestRunVerdict
    reason: str
    # Signal A value (weak / likely_test / unknown), mirroring the project-level
    # ``test_command_quality`` already exposed by project_responses.py.
    command_quality: str
    counts_parsed: bool
    total_tests: int | None
    passed_tests: int | None
    failed_tests: int | None
    zero_tests_detected: bool


# ANSI color / control escapes some runners emit. Stripped before parsing so a
# colored "5 passed" still parses. Pure regex; never raises.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")

# Zero-test markers. Each is anchored so a count like "10 selected" / "10 items"
# can never be misread as the zero form ("0 selected" / "0 items"). These are the
# only positive signals that 0 tests ran; a MISSING summary is never one of them.
_ZERO_TEST_MARKER_RES = (
    re.compile(r"collected\s+0\s+items"),          # pytest: collected 0 items
    re.compile(r"\b0\s+tests?\s+collected\b"),     # "0 tests collected"
    re.compile(r"\b0\s+selected\b"),               # pytest -k unmatched: 0 selected
    re.compile(r"no\s+tests\s+ran"),               # pytest: no tests ran
    re.compile(r"no\s+test\s+specified"),          # npm default stub
)


@dataclass(frozen=True)
class _Evidence:
    counts_parsed: bool
    total_tests: int | None
    passed_tests: int | None
    failed_tests: int | None
    zero_tests_detected: bool


def _parse_evidence(output: str | None) -> _Evidence:
    """
    Best-effort, conservative parse of pytest-style output. Pure; never raises.

    Returns parsed pass/fail counts and whether the output positively shows zero
    tests ran. Crucially, the absence of a parseable summary yields
    ``counts_parsed=False`` and ``zero_tests_detected=False`` — it is NOT inferred
    to mean zero tests.
    """
    text = _ANSI_ESCAPE_RE.sub("", output or "").lower()

    passed_match = _PASSED_RE.search(text)
    failed_match = _FAILED_RE.search(text)
    passed_tests = int(passed_match.group(1)) if passed_match else None
    failed_tests = int(failed_match.group(1)) if failed_match else None

    marker_zero = any(pattern.search(text) for pattern in _ZERO_TEST_MARKER_RES)

    # "0 passed" (explicit zero) also means no tests passed. A missing count does
    # not; that is the absence-is-not-zero rule.
    zero_tests_detected = marker_zero or passed_tests == 0

    if passed_tests is None and failed_tests is None:
        total_tests: int | None = None
    else:
        total_tests = (passed_tests or 0) + (failed_tests or 0)

    counts_parsed = (
        passed_tests is not None or failed_tests is not None or marker_zero
    )

    return _Evidence(
        counts_parsed=counts_parsed,
        total_tests=total_tests,
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        zero_tests_detected=zero_tests_detected,
    )


def _result(
    verdict: TestRunVerdict,
    reason: str,
    command_quality: TestCommandQualityResult,
    evidence: _Evidence,
) -> TestRunValidationResult:
    return TestRunValidationResult(
        verdict=verdict,
        reason=reason,
        command_quality=command_quality.quality.value,
        counts_parsed=evidence.counts_parsed,
        total_tests=evidence.total_tests,
        passed_tests=evidence.passed_tests,
        failed_tests=evidence.failed_tests,
        zero_tests_detected=evidence.zero_tests_detected,
    )


def classify_test_run(
    command: str | None,
    exit_code: int | None,
    output: str | None,
) -> TestRunValidationResult:
    """
    Classify a completed test run as STRONG / WEAK / NONE / UNKNOWN.

    Deterministic and side-effect free. Joins Signal A (command intent, via the
    existing ``classify_test_command``) with Signal B (execution evidence parsed
    from ``output``). The rules, in order:

      1. Blank/missing command            -> NONE.
      2. exit_code != 0 (failure branch)  -> never STRONG. The failure/rollback
         path owns this; we only label, never imply tests passed. A WEAK command
         stays WEAK; anything else is UNKNOWN.
      3. Command quality WEAK             -> WEAK, regardless of output.
      4. Command quality UNKNOWN          -> UNKNOWN (never accuse custom scripts).
      5. Command quality LIKELY_TEST (exit 0):
           - output shows 0 tests ran     -> WEAK.
           - exit 0 and >=1 passed, no failures reported -> STRONG.
           - >=1 passed but failures also reported (contradicts exit 0) -> UNKNOWN.
           - no parseable evidence        -> UNKNOWN (absence is not strength).

    STRONG is only ever reached with ``exit_code == 0``.
    """
    command_quality = classify_test_command(command or "")
    evidence = _parse_evidence(output)

    if not (command or "").strip():
        return _result(
            TestRunVerdict.NONE,
            "No test command configured; nothing validated this change.",
            command_quality,
            evidence,
        )

    # Failure branch: a non-zero exit is owned by the existing tester
    # failure/rollback path. We never return STRONG and never imply tests passed.
    if exit_code is not None and exit_code != 0:
        if command_quality.quality is TestCommandQuality.WEAK:
            return _result(
                TestRunVerdict.WEAK,
                f"{command_quality.reason} (Command also exited non-zero "
                f"[{exit_code}].)",
                command_quality,
                evidence,
            )
        return _result(
            TestRunVerdict.UNKNOWN,
            f"Test command exited non-zero [{exit_code}]; the failure path owns "
            "this, so it cannot be treated as strong validation.",
            command_quality,
            evidence,
        )

    # Success path (exit_code == 0, or None meaning "exit code not provided").
    if command_quality.quality is TestCommandQuality.WEAK:
        return _result(
            TestRunVerdict.WEAK,
            command_quality.reason,
            command_quality,
            evidence,
        )

    if command_quality.quality is TestCommandQuality.UNKNOWN:
        return _result(
            TestRunVerdict.UNKNOWN,
            "Pipewright cannot confirm this command runs your test suite; "
            "custom scripts are allowed but not treated as strong validation.",
            command_quality,
            evidence,
        )

    # command_quality.quality is LIKELY_TEST
    if evidence.zero_tests_detected:
        return _result(
            TestRunVerdict.WEAK,
            "The command is a recognized test runner but its output shows no "
            "tests ran (0 collected/selected/passed); this validated nothing.",
            command_quality,
            evidence,
        )

    has_passes = evidence.passed_tests is not None and evidence.passed_tests >= 1
    has_failures = evidence.failed_tests is not None and evidence.failed_tests >= 1

    if exit_code == 0 and has_passes and not has_failures:
        return _result(
            TestRunVerdict.STRONG,
            f"A recognized test runner exited 0 and {evidence.passed_tests} "
            "test(s) passed.",
            command_quality,
            evidence,
        )

    if has_passes and has_failures:
        # Failures reported alongside a success exit code is contradictory; refuse
        # to call it strong.
        return _result(
            TestRunVerdict.UNKNOWN,
            "Test output reports failures alongside a success exit code; the "
            "evidence is contradictory, so this cannot be treated as strong "
            "validation.",
            command_quality,
            evidence,
        )

    return _result(
        TestRunVerdict.UNKNOWN,
        "The command is a recognized test runner but its output gives no "
        "parseable evidence that tests ran; absence of a summary is not strong "
        "validation.",
        command_quality,
        evidence,
    )
