"""
run_outcome_suggestions.py
Deterministic run-outcome memory suggestion generator (#21D).

Turns *structured* run artifacts (run status, chunk completion summaries,
patch-failure reports, rejected approval reasons) into pending
`memory_suggestions`. It is intentionally narrow and safe:

  - Deterministic only. No LLM, no embeddings, no repo/log/stack-trace reading.
  - Reads only structured DB fields already produced by the pipeline.
  - Produces pending suggestions only — never an active memory fact, never an
    approval.
  - Reuses the existing content gate (validate_memory_content via
    insert_pending_suggestion) and the existing per-project content-hash dedupe.

A single failed run can therefore never become a durable injected rule: every
record stays pending until a human approves it through the existing
suggestion lifecycle (#21C).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import text

from backend.core.statuses import GateStatus, RunStatus
from backend.db.database import engine
from backend.memory.bootstrap import insert_pending_suggestion
from backend.memory.memory_store import DEFAULT_PRIORITY
from backend.pipeline.patch_failures import (
    PATCH_FAILURE_KIND,
    PatchFailureType,
    patch_failure_report_from_completion_summary,
)
from backend.projects.project_store import get_project

logger = logging.getLogger(__name__)

RUN_OUTCOME_SOURCE = "run_outcome"
RUN_OUTCOME_SOURCE_TYPE = "run_outcome"
RUN_HANDOFF_SOURCE_TYPE = "run_handoff"

# Conservative caps. validate_memory_content enforces the hard 400-char ceiling;
# these keep run-derived text well under it and leave room for the run-id
# prefix used by operational notes.
MAX_REJECTION_REASON_CHARS = 200
MAX_HANDOFF_ENTRY_CHARS = 280


# Deterministic operational notes per patch-failure enum value. Each entry is
# (content_template, category, risk_level). Templates only ever interpolate a
# short run reference — never a file path, raw log, or stack trace. Failure
# types that are not actionable as a memory note (PATCH_MALFORMED,
# PATCH_PARTIAL_APPLY_BLOCKED, NO_CHANGES, UNKNOWN_PATCH_FAILURE) are
# deliberately omitted (no suggestion).
_PATCH_FAILURE_NOTES: dict[PatchFailureType, tuple[str, str, str]] = {
    PatchFailureType.DIRTY_WORKTREE: (
        "Run {run_ref} hit a dirty worktree before patch application; clean or "
        "commit local changes before retrying.",
        "other",
        "low",
    ),
    PatchFailureType.STALE_INDEX_OR_FILE_CHANGED: (
        "Run {run_ref} hit a stale index or a changed target file; re-index the "
        "repo before retrying similar path-specific edits.",
        "other",
        "medium",
    ),
    PatchFailureType.TARGET_MISSING: (
        "Run {run_ref} targeted a file that no longer exists; verify the exact "
        "repo-relative path before retrying.",
        "other",
        "medium",
    ),
    PatchFailureType.PATCH_DOES_NOT_APPLY: (
        "Run {run_ref} produced a patch that did not apply cleanly; this came "
        "from one failed run and must be reviewed before approval.",
        "other",
        "medium",
    ),
    PatchFailureType.SCOPE_VIOLATION: (
        "Run {run_ref} attempted a change outside the approved chunk scope; "
        "review the scope boundaries before retrying.",
        "security",
        "high",
    ),
    PatchFailureType.FORBIDDEN_FILE: (
        "Run {run_ref} attempted to modify a protected file; review which files "
        "are allowed before retrying.",
        "security",
        "high",
    ),
    PatchFailureType.TEST_FAILURE_AFTER_APPLY: (
        "Run {run_ref} failed the project tests after applying changes; review "
        "the failing test names before retrying.",
        "test",
        "medium",
    ),
    PatchFailureType.TEST_REGRESSION: (
        "Run {run_ref} introduced or exposed a test regression after applying "
        "changes; review the failing test names before retrying.",
        "test",
        "medium",
    ),
    PatchFailureType.HARNESS_ERROR: (
        "Run {run_ref} could not get a trustworthy test result from the harness; "
        "check the test command and environment before retrying.",
        "test",
        "medium",
    ),
}


def _run_ref(run_id: str) -> str:
    """
    Short, stable run reference for *validated* content (like a git short hash).

    The full run_id lives in the unvalidated source_run_id/rationale provenance
    fields. Keeping only a short prefix in content avoids a digit-heavy UUID
    being misread as a phone number by the #21B safety gate, without weakening
    that gate. Per-project content-hash dedupe still works because the prefix is
    stable for a given run.
    """
    return run_id[:8]


@dataclass
class _Candidate:
    content: str
    category: str
    source_type: str
    rationale: str
    risk_level: str | None = None
    source_chunk_number: int | None = None
    scope: str = "global"
    priority: int = DEFAULT_PRIORITY


@dataclass
class RunSuggestionResult:
    run_id: str
    project_id: str
    generated: list[dict] = field(default_factory=list)
    skipped_count: int = 0
    blocked_count: int = 0

    @property
    def generated_count(self) -> int:
        return len(self.generated)


def _load_run_row(run_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, project_id, status
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    return dict(row._mapping) if row else None


def _load_chunks(run_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT chunk_number, status, completion_summary, error_message
            FROM chunks
            WHERE run_id = :run_id
            ORDER BY chunk_number ASC
        """), {"run_id": run_id}).fetchall()
    return [dict(row._mapping) for row in rows]


def _load_rejected_gates(run_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT rejection_reason
            FROM approval_gates
            WHERE run_id = :run_id
              AND status = :status
            ORDER BY created_at ASC
        """), {"run_id": run_id, "status": GateStatus.REJECTED}).fetchall()
    return [dict(row._mapping) for row in rows]


def _parse_summary(value) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_success_summary(value) -> dict | None:
    parsed = _parse_summary(value)
    if parsed is None or parsed.get("kind") == PATCH_FAILURE_KIND:
        return None
    return parsed


def _sanitize_reason(value) -> str:
    if not value:
        return ""
    collapsed = " ".join(str(value).split())
    return collapsed[:MAX_REJECTION_REASON_CHARS]


def _completed_run_candidates(run: dict, project: dict | None) -> list[_Candidate]:
    if run["status"] != RunStatus.COMPLETE or not project:
        return []
    test_command = (project.get("test_command") or "").strip()
    if not test_command:
        return []
    return [_Candidate(
        content=f"Project test command: {test_command}",
        category="test",
        source_type=RUN_OUTCOME_SOURCE_TYPE,
        rationale=(
            f"Run {run['id']} completed successfully using this project test "
            f"command."
        ),
    )]


def _handoff_candidates(run_id: str, chunks: list[dict]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for chunk in chunks:
        summary = _parse_success_summary(chunk["completion_summary"])
        if summary is None:
            continue
        entries = summary.get("suggested_memory_entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, str):
                continue
            value = entry.strip()
            # Conservative: skip empty or long entries rather than truncating a
            # model-suggested fact into something it did not mean.
            if not value or len(value) > MAX_HANDOFF_ENTRY_CHARS:
                continue
            candidates.append(_Candidate(
                content=value,
                category="other",
                source_type=RUN_HANDOFF_SOURCE_TYPE,
                source_chunk_number=chunk["chunk_number"],
                rationale=(
                    f"Suggested by the planner/coder handoff in run {run_id} "
                    f"chunk {chunk['chunk_number']}; review before approving."
                ),
            ))
    return candidates


def _patch_failure_candidates(run_id: str, chunks: list[dict]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for chunk in chunks:
        parsed = _parse_summary(chunk["completion_summary"])
        if parsed is None:
            continue
        report = patch_failure_report_from_completion_summary(parsed)
        if report is None:
            continue
        note = _PATCH_FAILURE_NOTES.get(report.failure_type)
        if note is None:
            continue
        template, category, risk_level = note
        candidates.append(_Candidate(
            content=template.format(run_ref=_run_ref(run_id)),
            category=category,
            source_type=RUN_OUTCOME_SOURCE_TYPE,
            source_chunk_number=chunk["chunk_number"],
            risk_level=risk_level,
            rationale=(
                f"Operational note from patch failure {report.failure_type.value} "
                f"in run {run_id} (chunk {chunk['chunk_number']}). From one "
                f"failed run only; review before approving."
            ),
        ))
    return candidates


def _rejection_candidates(run_id: str, gates: list[dict]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for gate in gates:
        reason = _sanitize_reason(gate.get("rejection_reason"))
        if not reason:
            continue
        candidates.append(_Candidate(
            content=f"Rejected approach in run {_run_ref(run_id)}: {reason}",
            category="other",
            source_type=RUN_OUTCOME_SOURCE_TYPE,
            risk_level="medium",
            rationale=(
                f"Captured from a rejected approval in run {run_id}; review "
                f"before approving."
            ),
        ))
    return candidates


def generate_run_memory_suggestions(
    run_id: str,
    *,
    requested_by: str = "user",
) -> RunSuggestionResult:
    """
    Generate pending memory suggestions from one run's structured artifacts.

    Idempotent: existing pending suggestions and active facts with the same
    content hash are skipped, so calling this repeatedly does not create
    duplicates. Raises ValueError if the run does not exist; fails closed
    (returns an empty result) if the run has no project_id.
    """
    run = _load_run_row(run_id)
    if run is None:
        raise ValueError("run_outcome_suggestions.py: run not found")

    project_id = (run.get("project_id") or "").strip()
    if not project_id:
        logger.warning(
            "run_outcome_suggestions.py: run %s has no project_id; failing closed",
            run_id,
        )
        return RunSuggestionResult(run_id=run_id, project_id="")

    project = get_project(project_id)
    chunks = _load_chunks(run_id)
    gates = _load_rejected_gates(run_id)

    candidates: list[_Candidate] = []
    candidates += _completed_run_candidates(run, project)
    candidates += _handoff_candidates(run_id, chunks)
    candidates += _patch_failure_candidates(run_id, chunks)
    candidates += _rejection_candidates(run_id, gates)

    result = RunSuggestionResult(run_id=run_id, project_id=project_id)
    for candidate in candidates:
        try:
            inserted = insert_pending_suggestion(
                project_id,
                candidate.content,
                category=candidate.category,
                scope=candidate.scope,
                priority=candidate.priority,
                source=RUN_OUTCOME_SOURCE,
                source_type=candidate.source_type,
                source_run_id=run_id,
                source_chunk_number=candidate.source_chunk_number,
                rationale=candidate.rationale,
                suggested_by=requested_by,
                risk_level=candidate.risk_level,
            )
        except ValueError:
            # Blocked by the #21B write-path gate (control-plane phrase, secret,
            # absolute path, stack trace, large code block). Never stored.
            result.blocked_count += 1
            continue
        if inserted is None:
            result.skipped_count += 1
        else:
            result.generated.append(inserted)

    return result
