"""
test_fail_chunk_report.py
Narrow persistence test for #26C: a forced patch failure routed through
_fail_chunk_with_report persists a failure_report_id and an initial attempt,
while status/error/result behavior stays unchanged.

Persistence and event sinks are monkeypatched so this stays a pure unit test
(no DB, no event bus). The real record_initial_attempt + serialization run.
"""

import pytest

import backend.pipeline.chunked_orchestrator as orch
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
)

pytestmark = pytest.mark.unit


def test_fail_chunk_with_report_persists_failure_report_id_and_initial_attempt(
    monkeypatch,
):
    captured: dict = {}

    monkeypatch.setattr(
        orch,
        "save_chunk_completion_summary",
        lambda run_id, chunk_number, summary: captured.update(summary=summary),
    )
    monkeypatch.setattr(
        orch,
        "update_chunk_status",
        lambda *args, **kwargs: captured.update(status_args=args),
    )
    monkeypatch.setattr(orch, "_update_run_status", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_publish_safe", lambda event: None)

    report = build_patch_failure_report(
        PatchFailureType.PATCH_DOES_NOT_APPLY,
        changed_files_attempted=["a.py"],
        allowed_files=["a.py"],
        max_attempts=2,
        chunk_number=2,
    )

    result = orch._fail_chunk_with_report("run-1", 2, report)

    summary = captured["summary"]
    # Enriched, still the same top-level discriminated shape.
    assert summary["kind"] == "patch_failure"
    assert summary["failure_type"] == "PATCH_DOES_NOT_APPLY"
    assert summary["failure_report_id"]  # present and non-empty
    assert len(summary["attempts"]) == 1
    attempt = summary["attempts"][0]
    assert attempt["attempt_number"] == 1
    assert attempt["recovery_mode"] == "initial"
    assert attempt["outcome"] == "failed"

    # Status/error/result behavior unchanged: still "failed" with the report message.
    assert captured["status_args"] == ("run-1", 2, "failed", report.message)
    assert result["status"] == "failed"
    assert result["failed_chunk"] == 2
    assert result["error"] == report.message
