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
from pathlib import Path

from backend.repo.repo_fingerprint import load_repo_file

# A make target named exactly "test" at column 0 ("test:" / "test : deps").
# Indented lines are recipe text, not targets, and never match.
_MAKE_TEST_TARGET_RE = re.compile(r"^test\s*:")

# Python dependency manifests consulted for the Django signal.
_PYTHON_DEP_MANIFESTS = ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")


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


def detect_test_command(repo_path) -> str | None:
    """
    Suggest a test command for the repo at ``repo_path``, or None.

    Checks root-level marker files in priority order and returns the first
    match:

      1. ``pytest.ini`` present, or ``pyproject.toml`` with a ``[tool.pytest``
         section                                            -> ``pytest``
      2. ``setup.cfg`` with a ``[tool:pytest]`` section      -> ``pytest``
      3. ``package.json`` with ``jest`` in (dev)dependencies -> ``npm test``
      4. ``package.json`` with ``vitest`` in (dev)dependencies
                                                             -> ``npx vitest run``
      5. ``Makefile`` with a ``test:`` target                -> ``make test``
      6. ``manage.py`` present + Django in a Python manifest
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

    if load_repo_file(root, "pytest.ini") is not None:
        return "pytest"

    pyproject = load_repo_file(root, "pyproject.toml")
    if pyproject is not None and _has_config_section(pyproject, "[tool.pytest"):
        return "pytest"

    setup_cfg = load_repo_file(root, "setup.cfg")
    if setup_cfg is not None and _has_config_section(setup_cfg, "[tool:pytest]"):
        return "pytest"

    package_json = load_repo_file(root, "package.json")
    if package_json is not None:
        if _package_json_has_dependency(package_json, "jest"):
            return "npm test"
        if _package_json_has_dependency(package_json, "vitest"):
            return "npx vitest run"

    makefile = load_repo_file(root, "Makefile")
    if makefile is not None and any(
        _MAKE_TEST_TARGET_RE.match(line) for line in makefile.splitlines()
    ):
        return "make test"

    if load_repo_file(root, "manage.py") is not None and _has_django_dependency(root):
        return "python manage.py test"

    return None
