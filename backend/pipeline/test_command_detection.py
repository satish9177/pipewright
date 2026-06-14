"""
test_command_detection.py
Deterministic test-command detector for project setup (Phase 0, G4).

Why this exists
---------------
New projects have no ``test_command`` configured; the user must already know
and type it. This module scans well-known marker files at the repo root and
returns a suggested command string the setup UI / first-run pre-flight can
prefill for the human to confirm or edit.

DETECT AND PREFILL ONLY — this module NEVER executes a command, and the
suggestion is never applied without the human confirming it.

It is pure and deterministic: no LLM, no DB, no subprocess. File reads go
through ``repo_fingerprint.load_repo_file`` (traversal-safe, size-capped,
refuses forbidden paths such as ``.env``). Precision over recall: a runner
that is merely *mentioned* (a comment, a script string, broken JSON) is not a
signal; when nothing matches confidently the answer is None, never a guess.

Relationship to test_command_quality.py: that module is the pure *classifier*
of an already-configured command string (no filesystem by contract); this one
is the *detector* that proposes a command from repo state. The shared
rules-as-data table unifying both is a later refactor (proposal §14).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.repo.repo_fingerprint import load_repo_file

# A make target named exactly "test" at column 0 ("test:" / "test : deps").
# Indented lines are recipe text, not targets, and never match.
_MAKE_TEST_TARGET_RE = re.compile(r"^test\s*:")

# Python dependency manifests consulted for the Django signal.
_PYTHON_DEP_MANIFESTS = ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")

# Keep detector package literals local for now. Physical vocabulary unification
# is deferred because this detector returns root-only first-match command strings,
# while repo reality returns canonical values from manifest walking and omits
# ambiguous dimensions.
_JEST_PACKAGE_NAME = "jest"
_VITEST_PACKAGE_NAME = "vitest"


@dataclass(frozen=True)
class TestCommandRule:
    name: str
    command: str
    matches: Callable[[Path], bool]


def _has_config_section(content: str, section_prefix: str) -> bool:
    """True when a line is a real config section header, not a comment."""
    for line in content.splitlines():
        if line.strip().startswith(section_prefix):
            return True
    return False


def _package_json_has_dependency(content: str, package_name: str) -> bool:
    """
    True when ``package_name`` is a key of dependencies/devDependencies.
    Broken JSON is not a signal (precision over recall — never guess).
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key) or {}
        if isinstance(deps, dict) and package_name in deps:
            return True
    return False


def _has_django_dependency(root: Path) -> bool:
    for manifest in _PYTHON_DEP_MANIFESTS:
        content = load_repo_file(root, manifest)
        if content is not None and "django" in content.lower():
            return True
    return False


def _has_root_file(root: Path, filename: str) -> bool:
    return load_repo_file(root, filename) is not None


def _pyproject_has_pytest_section(root: Path) -> bool:
    pyproject = load_repo_file(root, "pyproject.toml")
    return pyproject is not None and _has_config_section(pyproject, "[tool.pytest")


def _setup_cfg_has_pytest_section(root: Path) -> bool:
    setup_cfg = load_repo_file(root, "setup.cfg")
    return setup_cfg is not None and _has_config_section(setup_cfg, "[tool:pytest]")


def _package_json_has_test_dependency(root: Path, package_name: str) -> bool:
    package_json = load_repo_file(root, "package.json")
    return (
        package_json is not None
        and _package_json_has_dependency(package_json, package_name)
    )


def _makefile_has_test_target(root: Path) -> bool:
    makefile = load_repo_file(root, "Makefile")
    return makefile is not None and any(
        _MAKE_TEST_TARGET_RE.match(line) for line in makefile.splitlines()
    )


def _has_django_test_signal(root: Path) -> bool:
    return _has_root_file(root, "manage.py") and _has_django_dependency(root)


_TEST_COMMAND_RULES = (
    TestCommandRule(
        name="pytest_ini",
        command="pytest",
        matches=lambda root: _has_root_file(root, "pytest.ini"),
    ),
    TestCommandRule(
        name="pyproject_pytest",
        command="pytest",
        matches=_pyproject_has_pytest_section,
    ),
    TestCommandRule(
        name="setup_cfg_pytest",
        command="pytest",
        matches=_setup_cfg_has_pytest_section,
    ),
    TestCommandRule(
        name="package_json_jest",
        command="npm test",
        matches=lambda root: _package_json_has_test_dependency(
            root, _JEST_PACKAGE_NAME
        ),
    ),
    TestCommandRule(
        name="package_json_vitest",
        command="npx vitest run",
        matches=lambda root: _package_json_has_test_dependency(
            root, _VITEST_PACKAGE_NAME
        ),
    ),
    TestCommandRule(
        name="makefile_test_target",
        command="make test",
        matches=_makefile_has_test_target,
    ),
    TestCommandRule(
        name="django_manage_py",
        command="python manage.py test",
        matches=_has_django_test_signal,
    ),
)


def detect_test_command(repo_path) -> str | None:
    """
    Suggest a test command for the repo at ``repo_path``, or None.

    Checks root-level marker files in priority order and returns the first
    match:

      1. ``pytest.ini`` present                             -> ``pytest``
      2. ``pyproject.toml`` with a ``[tool.pytest`` section  -> ``pytest``
      3. ``setup.cfg`` with a ``[tool:pytest]`` section      -> ``pytest``
      4. ``package.json`` with ``jest`` in (dev)dependencies -> ``npm test``
      5. ``package.json`` with ``vitest`` in (dev)dependencies
                                                             -> ``npx vitest run``
      6. ``Makefile`` with a ``test:`` target                -> ``make test``
      7. ``manage.py`` present + Django in a Python manifest
                                                             -> ``python manage.py test``

    Returns None for a missing/invalid path or when no signal is found. The
    returned string is a SUGGESTION for the human to confirm; nothing is ever
    executed here.
    """
    try:
        root = Path(repo_path).resolve()
    except (OSError, ValueError, TypeError):
        return None
    if not root.exists() or not root.is_dir():
        return None

    for rule in _TEST_COMMAND_RULES:
        if rule.matches(root):
            return rule.command

    return None
