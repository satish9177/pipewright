"""
scope_guard.py
Strict scope-drift guard for chunk execution.

Compares each coder-produced file path against the chunk's approved
files_expected. Strict exact-path matching after normalization. Raises
ScopeDriftError on the first violation, before apply_patch, run_tests,
or commit_files. Empty files_expected is treated as unsafe and blocked.
"""

from __future__ import annotations

from backend.models.handoff import CoderHandoff
from backend.utils.path_safety import normalize_relative_path


class ScopeDriftError(RuntimeError):
    """Raised when coder output would write outside the approved scope."""


def _safe_normalize(path: str, *, role: str) -> str:
    try:
        return normalize_relative_path(path)
    except Exception as error:
        raise ScopeDriftError(
            f"scope_guard.py: invalid {role} path {path!r}: {error}"
        ) from error


def assert_files_in_scope(
    coder_output: CoderHandoff,
    files_expected: list[str],
) -> None:
    """
    Block if any coder-produced path is not exactly in files_expected.

    - Strict exact-path match after normalization (forward slashes,
      no traversal, no absolute paths).
    - No wildcard / directory-prefix matching.
    - Empty files_expected is treated as unsafe and blocked.
    """
    if not files_expected:
        raise ScopeDriftError(
            "scope_guard.py: chunk has empty files_expected; "
            "cannot safely apply patch."
        )

    expected: set[str] = set()
    for entry in files_expected:
        expected.add(_safe_normalize(entry, role="files_expected"))

    for change in coder_output.files_changed:
        actual = _safe_normalize(change.path, role="coder")
        if actual not in expected:
            raise ScopeDriftError(
                "scope_guard.py: coder wrote file outside approved scope. "
                f"path={actual} | files_expected={sorted(expected)}"
            )
