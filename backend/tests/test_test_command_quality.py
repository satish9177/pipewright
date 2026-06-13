"""
test_test_command_quality.py
Unit tests for the deterministic test-command quality classifier (#23A).

Pure string classification: no execution, no filesystem, no DB.
"""

import pytest

from backend.pipeline.test_command_quality import (
    TestCommandQuality,
    classify_test_command,
)

pytestmark = pytest.mark.unit


def _quality(command: str) -> TestCommandQuality:
    return classify_test_command(command).quality


# --------------------------------------------------------------------------
# Weak commands
# --------------------------------------------------------------------------

WEAK_COMMANDS = [
    # version / inspection probes
    "python --version",
    "python -V",
    "python3 --version",
    "node --version",
    "npm --version",
    "pip --version",
    "pip3 --version",
    "pnpm --version",
    "yarn --version",
    "go version",
    "cargo --version",
    "java --version",
    "mvn --version",
    "gradle --version",
    "docker --version",
    "git --version",
    # no-op / shell inspection
    "true",
    ":",
    "echo ok",
    "pwd",
    "ls",
    "dir",
    "cd ..",
    "cat README.md",
    "whoami",
    "date",
    "sleep 5",
    # git inspection only
    "git status",
    "git log",
    "git diff",
    "git branch",
    # bare interpreters
    "python",
    "python3",
    "node",
]


@pytest.mark.parametrize("command", WEAK_COMMANDS)
def test_weak_commands_classify_weak(command):
    result = classify_test_command(command)
    assert result.quality is TestCommandQuality.WEAK
    assert result.reason  # always carries a human-readable reason


# --------------------------------------------------------------------------
# Likely test commands
# --------------------------------------------------------------------------

LIKELY_TEST_COMMANDS = [
    "pytest",
    "py.test",
    r"C:\Users\satis\AppData\Local\Programs\Python\Python311\python.exe -m pytest backend/tests/test_handoff_models.py -q",
    "/usr/bin/python3 -m pytest",
    "python -m pytest",
    "python3 -m pytest",
    "python -m unittest",
    "tox",
    "nox",
    "npm test",
    "npm.cmd test",
    "npm run test",
    "npm run test:unit",
    "npm run test:ci",
    "pnpm test",
    "pnpm.cmd test",
    "pnpm run test",
    "yarn test",
    "yarn.cmd test",
    "vitest",
    "jest",
    "mocha",
    "mvn test",
    "mvnw test",
    "./mvnw test",
    r".\mvnw.cmd test",
    "mvn verify",
    "gradle test",
    "./gradlew test",
    "gradlew.bat test",
    "go test ./...",
    "cargo test",
    "npx jest",
    "npx vitest",
    "poetry run pytest",
    "pipenv run pytest",
    "uv run pytest",
    "pdm run pytest",
    "rspec",
    "rake test",
    "phpunit",
    "dotnet test",
    "bin/rails test",
    "make test",
    "make check",
    # with extra arguments
    "pytest -q tests/",
    "python -m pytest backend/tests -m unit",
    "jest --coverage",
    "go test ./... -run TestThing",
]


@pytest.mark.parametrize("command", LIKELY_TEST_COMMANDS)
def test_likely_test_commands_classify_likely(command):
    result = classify_test_command(command)
    assert result.quality is TestCommandQuality.LIKELY_TEST
    assert result.reason


@pytest.mark.parametrize("command", [
    "pytest.exe -q",
    "npm.cmd test",
    "yarn.bat test",
    r".\mvnw.cmd test",
    r"C:\tools\gradle\bin\gradle.bat test",
])
def test_head_path_and_suffix_normalization_classifies_likely_tests(command):
    assert _quality(command) is TestCommandQuality.LIKELY_TEST


@pytest.mark.parametrize("command", [
    "mvnw test",
    "./mvnw test",
    r".\mvnw.cmd test",
    "npx jest",
    "npx vitest",
    "poetry run pytest",
    "pipenv run pytest",
    "uv run pytest",
    "pdm run pytest",
])
def test_common_test_wrappers_classify_likely(command):
    assert _quality(command) is TestCommandQuality.LIKELY_TEST


# --------------------------------------------------------------------------
# Unknown commands (must NOT be weak)
# --------------------------------------------------------------------------

UNKNOWN_COMMANDS = [
    "make build",
    "npm run check",
    "npm run lint",
    "./scripts/ci.sh",
    "bash run_tests.sh",
    "./custom-command",
    "go build",
    "cargo build",
    "mvn package",
    "java -jar app.jar",
]


@pytest.mark.parametrize("command", UNKNOWN_COMMANDS)
def test_unknown_commands_classify_unknown(command):
    result = classify_test_command(command)
    assert result.quality is TestCommandQuality.UNKNOWN
    assert result.quality is not TestCommandQuality.WEAK
    assert result.reason


@pytest.mark.parametrize("command", [
    "python script.py",
    r"C:\Users\satis\AppData\Local\Programs\Python\Python311\python.exe script.py",
    "node server.js",
    "echo hello",
])
def test_generic_commands_do_not_become_likely_tests(command):
    assert _quality(command) is not TestCommandQuality.LIKELY_TEST


# --------------------------------------------------------------------------
# Chained commands
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("python --version && pytest", TestCommandQuality.LIKELY_TEST),
    ("echo ok ; true", TestCommandQuality.WEAK),
    ("pytest && echo done", TestCommandQuality.LIKELY_TEST),
    ("npm --version && npm test", TestCommandQuality.LIKELY_TEST),
    ("pip install -r requirements.txt && pytest", TestCommandQuality.LIKELY_TEST),
    ("echo start | grep start", TestCommandQuality.UNKNOWN),  # grep is unknown
    ("true && true", TestCommandQuality.WEAK),
    ("make build && npm run lint", TestCommandQuality.UNKNOWN),
])
def test_chained_commands_combine(command, expected):
    assert _quality(command) is expected


# --------------------------------------------------------------------------
# Normalization + edge cases
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("  PyTest  ", TestCommandQuality.LIKELY_TEST),
    ("PYTHON --VERSION", TestCommandQuality.WEAK),
    ("npm    test", TestCommandQuality.LIKELY_TEST),
    ("\tnpm\trun\ttest\t", TestCommandQuality.LIKELY_TEST),
])
def test_whitespace_and_case_normalization(command, expected):
    assert _quality(command) is expected


@pytest.mark.parametrize("command", ["", "   ", "\t\n", None])
def test_empty_or_blank_command_is_unknown(command):
    result = classify_test_command(command)  # type: ignore[arg-type]
    assert result.quality is TestCommandQuality.UNKNOWN
    assert result.reason


def test_trailing_operator_is_handled_safely():
    # Empty trailing segment must be ignored, not crash or flip the result.
    assert _quality("pytest &&") is TestCommandQuality.LIKELY_TEST
    assert _quality("python --version ;") is TestCommandQuality.WEAK


def test_classifier_does_not_execute_anything(monkeypatch):
    # Defense-in-depth: the classifier must never shell out.
    import subprocess

    def _boom(*args, **kwargs):
        raise AssertionError("classifier must not execute commands")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "call", _boom)

    assert _quality("python --version") is TestCommandQuality.WEAK
    assert _quality("pytest") is TestCommandQuality.LIKELY_TEST
