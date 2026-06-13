"""
test_command_quality.py
Deterministic classifier for a project's configured test command (#23A).

Why this exists
---------------
Pipewright's safety model treats "test command exited 0" as "tests passed" and
saves a ``tests_passed=True`` checkpoint. A command like ``python --version``
exits 0 but runs no tests, so a run can look validated when it was not.

This module classifies a configured ``test_command`` string into:
  - ``WEAK``        — a recognized no-op / version / inspection command that
                      clearly does not run tests (e.g. ``python --version``).
  - ``LIKELY_TEST`` — a recognized test runner (e.g. ``pytest``, ``npm test``).
  - ``UNKNOWN``     — anything else, including custom scripts. Deliberately NOT
                      weak: when uncertain we never falsely accuse a real setup.

It is pure and deterministic: it reads ONLY the command string. No LLM, no
filesystem access, no command execution, no DB. It does not change execution,
checkpoint, rollback, or final-approval behavior — it only labels a string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TestCommandQuality(str, Enum):
    # Name begins with "Test", so tell pytest not to try to collect it as a
    # test class (a dunder, so it is not an enum member).
    __test__ = False

    WEAK = "weak"
    LIKELY_TEST = "likely_test"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TestCommandQualityResult:
    quality: TestCommandQuality
    reason: str


# Shell operators that separate independent commands. A command is the
# combination of its segments (see classify_test_command for the combine rule).
# ``||`` must precede ``|`` in the alternation so it is matched first.
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")

# Single-token commands that are test runners regardless of their arguments.
_SIMPLE_TEST_RUNNERS = {
    "pytest", "py.test", "tox", "nox",
    "vitest", "jest", "mocha",
    "rspec", "phpunit",
}

# Version / "print info" flags. The command is lowercased before classification,
# so ``-V`` arrives here as ``-v`` (both mean "print version" for these heads).
_VERSION_FLAG_TOKENS = {"--version", "-v", "-version", "version"}

# Heads whose only job, with a version flag, is to print a version string.
_VERSION_PROBE_HEADS = {
    "python", "python3", "node", "npm", "pip", "pip3", "pnpm", "yarn",
    "go", "cargo", "java", "mvn", "gradle", "docker", "git",
}

# No-op / shell-inspection commands that never run tests (any args).
_NOOP_HEADS = {
    "true", ":", "echo", "pwd", "ls", "dir", "cd", "cat", "whoami",
    "date", "sleep",
}

# git subcommands that only inspect the repo.
_GIT_INSPECT_SUBCOMMANDS = {"status", "log", "diff", "branch"}

# Bare interpreters (no args) that just open a REPL.
_BARE_INTERPRETERS = {"python", "python3", "node"}


def _normalize_head_token(token: str) -> str:
    """Normalize an executable token without interpreting or executing it."""
    value = token.strip("\"'")
    value = value.replace("\\", "/")
    value = value.rsplit("/", 1)[-1]
    for suffix in (".exe", ".cmd", ".bat"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _normalize_segment(segment: str) -> str:
    """Trim and collapse internal whitespace. Input is already lowercased."""
    return " ".join(segment.split())


def _is_likely_test_segment(head: str, rest: list[str]) -> bool:
    """True when a normalized segment invokes a recognized test runner."""
    if head in _SIMPLE_TEST_RUNNERS:
        return True

    second = rest[0] if rest else ""
    third = rest[1] if len(rest) > 1 else ""

    if head in {"python", "python3"}:
        # python -m pytest / python -m unittest (target after -m)
        if "-m" in rest:
            module_index = rest.index("-m")
            if module_index + 1 < len(rest):
                return rest[module_index + 1] in {"pytest", "unittest"}
        return False

    if head in {"npm", "pnpm", "yarn"}:
        # npm test / npm run test* / yarn test:unit / pnpm run test:ci
        if second == "test" or second.startswith("test:"):
            return True
        if second == "run" and third.startswith("test"):
            return True
        return False

    if head in {"npx", "poetry", "pipenv", "uv", "pdm"}:
        if head == "npx":
            return second in {"jest", "vitest"}
        if second == "run":
            return third in _SIMPLE_TEST_RUNNERS or third in {"pytest", "unittest"}
        return False

    if head in {"go", "cargo", "dotnet", "rake"}:
        return second == "test"  # go test ./..., cargo test, dotnet test, rake test

    if head in {"mvn", "mvnw"}:
        return second in {"test", "verify"}

    if head in {"gradle", "gradlew"}:
        return second == "test"

    if head == "make":
        return second in {"test", "check"}

    if head in {"bin/rails", "rails"}:
        return second == "test"

    return False


def _weak_reason(head: str, rest: list[str]) -> str | None:
    """Return a reason string when a segment is a recognized weak command."""
    second = rest[0] if rest else ""

    if head == "git":
        if second in _GIT_INSPECT_SUBCOMMANDS:
            return f"'git {second}' inspects the repo; it does not run tests."
        if second in _VERSION_FLAG_TOKENS:
            return "'git --version' only prints a version; it does not run tests."
        return None

    if head in _VERSION_PROBE_HEADS and any(
        token in _VERSION_FLAG_TOKENS for token in rest
    ):
        return f"'{head}' with a version flag only prints a version; it does not run tests."

    if head in _NOOP_HEADS:
        return f"'{head}' is a no-op or inspection command; it does not run tests."

    if not rest and head in _BARE_INTERPRETERS:
        return f"'{head}' with no arguments starts a REPL; it does not run tests."

    return None


def _classify_segment(segment: str) -> tuple[TestCommandQuality, str]:
    """Classify a single normalized, non-empty segment."""
    tokens = segment.split()
    head = _normalize_head_token(tokens[0])
    rest = tokens[1:]

    # Likely-test is checked first: e.g. "python -m pytest" is a test runner
    # even though bare "python" is weak.
    if _is_likely_test_segment(head, rest):
        return TestCommandQuality.LIKELY_TEST, f"'{segment}' runs project tests."

    weak_reason = _weak_reason(head, rest)
    if weak_reason is not None:
        return TestCommandQuality.WEAK, weak_reason

    return (
        TestCommandQuality.UNKNOWN,
        f"'{segment}' does not match a known test runner or weak-command pattern.",
    )


def classify_test_command(command: str) -> TestCommandQualityResult:
    """
    Classify a configured test command as WEAK, LIKELY_TEST, or UNKNOWN.

    Deterministic and string-only. Splits shell chains (``&&``, ``||``, ``;``,
    ``|``) into segments and combines:
      - if ANY segment is a test runner -> LIKELY_TEST
      - else if every non-empty segment is weak -> WEAK
      - else -> UNKNOWN

    An empty/blank command is UNKNOWN (it cannot be confirmed to run tests, and
    is never falsely labeled a recognized weak command).
    """
    lowered = (command or "").lower()
    results: list[tuple[TestCommandQuality, str]] = []
    for raw_segment in _SEGMENT_SPLIT_RE.split(lowered):
        segment = _normalize_segment(raw_segment)
        if not segment:
            continue
        results.append(_classify_segment(segment))

    if not results:
        return TestCommandQualityResult(
            TestCommandQuality.UNKNOWN,
            "Empty or blank command; cannot confirm it runs tests.",
        )

    for quality, reason in results:
        if quality is TestCommandQuality.LIKELY_TEST:
            return TestCommandQualityResult(TestCommandQuality.LIKELY_TEST, reason)

    if all(quality is TestCommandQuality.WEAK for quality, _ in results):
        return TestCommandQualityResult(TestCommandQuality.WEAK, results[0][1])

    for quality, reason in results:
        if quality is TestCommandQuality.UNKNOWN:
            return TestCommandQualityResult(TestCommandQuality.UNKNOWN, reason)

    return TestCommandQualityResult(
        TestCommandQuality.UNKNOWN,
        "Command does not clearly run a recognized test runner.",
    )
