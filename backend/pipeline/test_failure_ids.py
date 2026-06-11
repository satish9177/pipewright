"""
Pure failing-test identity extraction for baseline-aware verification.

Phase 1 item 9 starts with pytest node IDs from short-summary lines:
``FAILED tests/test_app.py::test_name - AssertionError``.
When the output proves failures happened but identities cannot be recovered, the
parser marks the result non-comparable so callers can fail safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FailingTestIdParseResult:
    failed_ids: frozenset[str]
    parseable: bool
    reason: str
    expected_failed_count: int | None = None


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_FAILED_COUNT_RE = re.compile(r"\b(\d+)\s+failed\b", re.IGNORECASE)
_FAILED_LINE_RE = re.compile(r"^\s*FAILED\s+(?P<body>.+?)\s*$", re.IGNORECASE)


def extract_pytest_failing_test_ids(output: str | None) -> FailingTestIdParseResult:
    """Extract pytest node IDs from output. Pure and conservative."""
    text = _ANSI_ESCAPE_RE.sub("", output or "")
    failed_counts = [int(match.group(1)) for match in _FAILED_COUNT_RE.finditer(text)]
    expected_failed_count = max(failed_counts) if failed_counts else None

    failed_line_seen = False
    failed_ids: set[str] = set()
    for line in text.splitlines():
        match = _FAILED_LINE_RE.match(line)
        if not match:
            continue
        failed_line_seen = True
        body = match.group("body").strip()
        node_id = re.split(r"\s+-\s+", body, maxsplit=1)[0].strip()
        if "::" in node_id:
            failed_ids.add(node_id)

    if expected_failed_count is None and not failed_line_seen:
        return FailingTestIdParseResult(
            failed_ids=frozenset(),
            parseable=True,
            reason="no pytest failure evidence",
            expected_failed_count=None,
        )

    if not failed_ids:
        return FailingTestIdParseResult(
            failed_ids=frozenset(),
            parseable=False,
            reason="pytest failures were reported but no node IDs were parseable",
            expected_failed_count=expected_failed_count,
        )

    if expected_failed_count is not None and len(failed_ids) < expected_failed_count:
        return FailingTestIdParseResult(
            failed_ids=frozenset(sorted(failed_ids)),
            parseable=False,
            reason=(
                "pytest failure count exceeded the number of parseable node IDs"
            ),
            expected_failed_count=expected_failed_count,
        )

    return FailingTestIdParseResult(
        failed_ids=frozenset(sorted(failed_ids)),
        parseable=True,
        reason="parsed pytest failing node IDs",
        expected_failed_count=expected_failed_count,
    )
