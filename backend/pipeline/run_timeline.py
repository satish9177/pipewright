from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from backend.db.database import engine


CATEGORY_ORDER = {
    "lifecycle": 0,
    "plan": 10,
    "execution": 20,
    "review": 30,
    "approval": 40,
    "memory": 50,
    "ship": 60,
}

KIND_ORDER = {
    "run_created": 0,
    "run_status_changed": 10,
    "plan_created": 20,
    "chunk_created": 30,
    "chunk_started": 31,
    "chunk_completed": 32,
    "stage_completed": 40,
    "review_recorded": 50,
    "approval_required": 60,
    "approval_decided": 61,
    "scope_expansion_requested": 70,
    "scope_expansion_decided": 71,
    "acknowledgement_recorded": 80,
    "memory_injected": 90,
    "git_pushed": 100,
    "pr_created": 110,
}

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
]

_TECHNICAL_PAYLOAD_MARKERS = (
    "traceback (most recent call last)",
    "diff --git",
    "\n@@ ",
    "\n+++ ",
    "\n--- ",
    "fatal:",
)

@dataclass(frozen=True)
class TimelineEntry:
    id: str
    ts: str
    kind: str
    stage: str | None
    chunk_number: int | None
    level: str
    category: str
    title: str
    detail: str | None
    source: str = "persisted"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
            "stage": self.stage,
            "chunk_number": self.chunk_number,
            "level": self.level,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "source": self.source,
            "data": self.data,
        }


def build_run_timeline(run_id: str) -> list[dict[str, Any]] | None:
    """Derive a read-only run timeline from persisted pipeline tables."""
    with engine.connect() as conn:
        run = conn.execute(
            text(
                """
                SELECT id, project_id, status, current_step, intent, chunk_plan_status,
                       total_chunks, current_chunk_number, pr_number, branch_name,
                       pushed_at, pr_created_at, created_at
                FROM pipeline_runs
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id},
        ).mappings().first()
        if run is None:
            return None

        entries: list[TimelineEntry] = []
        _add_run_entries(entries, run)
        _add_plan_version_entries(entries, conn, run_id)
        _add_approval_gate_entries(entries, conn, run_id)
        _add_chunk_entries(entries, conn, run_id)
        _add_chunk_attempt_entries(entries, conn, run_id)
        _add_scope_expansion_entries(entries, conn, run_id)
        _add_test_ack_entries(entries, conn, run_id)
        _add_review_ack_entries(entries, conn, run_id)
        _add_chunk_review_entries(entries, conn, run_id)
        _add_memory_injection_entries(entries, conn, run_id)

    entries.sort(key=_entry_sort_key)
    return [entry.to_dict() for entry in entries]


def _add_run_entries(entries: list[TimelineEntry], run: dict[str, Any]) -> None:
    status = _safe_str(run.get("status")) or "unknown"
    _append(
        entries,
        entry_id="run:created",
        ts=run.get("created_at"),
        kind="run_created",
        stage="run",
        chunk_number=None,
        level="info",
        category="lifecycle",
        title="Run created",
        detail="Pipeline run persisted.",
        data={
            "run_id": run.get("id"),
            "project_id": run.get("project_id"),
            "status": status,
            "current_step": run.get("current_step"),
            "intent": run.get("intent"),
            "chunk_plan_status": run.get("chunk_plan_status"),
            "total_chunks": run.get("total_chunks"),
            "current_chunk_number": run.get("current_chunk_number"),
        },
    )
    if run.get("pushed_at"):
        _append(
            entries,
            entry_id="run:git:pushed",
            ts=run.get("pushed_at"),
            kind="git_pushed",
            stage="git",
            chunk_number=None,
            level="info",
            category="ship",
            title="Branch pushed",
            detail="Persisted push timestamp recorded.",
            data={
                "run_id": run.get("id"),
                "branch_name": run.get("branch_name"),
            },
        )
    if run.get("pr_created_at"):
        _append(
            entries,
            entry_id="run:pr:created",
            ts=run.get("pr_created_at"),
            kind="pr_created",
            stage="github_pr",
            chunk_number=None,
            level="info",
            category="ship",
            title="Pull request created",
            detail="Persisted pull request timestamp recorded.",
            data={
                "run_id": run.get("id"),
                "pr_number": run.get("pr_number"),
                "branch_name": run.get("branch_name"),
            },
        )


def _add_plan_version_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, version, source, created_from_turn_id, created_at
            FROM plan_versions
            WHERE run_id = :run_id
            ORDER BY created_at, version, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        version = row.get("version")
        _append(
            entries,
            entry_id=f"plan_version:{version}:{row.get('id')}",
            ts=row.get("created_at"),
            kind="plan_created",
            stage="planning",
            chunk_number=None,
            level="info",
            category="plan",
            title=f"Plan version {version} created",
            detail="Chunk plan version persisted.",
            data={
                "plan_version_id": row.get("id"),
                "version": version,
                "source": row.get("source"),
                "created_from_turn_id": row.get("created_from_turn_id"),
            },
        )


def _add_approval_gate_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, step, status, risk_level, chunk_number, approval_type,
                   decided_at, created_at
            FROM approval_gates
            WHERE run_id = :run_id
            ORDER BY created_at, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        gate_id = row.get("id")
        chunk_number = _int_or_none(row.get("chunk_number"))
        _append(
            entries,
            entry_id=f"approval_gate:{gate_id}:created",
            ts=row.get("created_at"),
            kind="approval_required",
            stage=row.get("step") or "approval",
            chunk_number=chunk_number,
            level="warn" if row.get("status") == "pending" else "info",
            category=_approval_gate_category(row),
            title="Approval gate recorded",
            detail="Human approval gate persisted.",
            data={
                "approval_gate_id": gate_id,
                "step": row.get("step"),
                "status": row.get("status"),
                "risk_level": row.get("risk_level"),
                "approval_type": row.get("approval_type"),
            },
        )
        if row.get("decided_at"):
            _append(
                entries,
                entry_id=f"approval_gate:{gate_id}:decided",
                ts=row.get("decided_at"),
                kind="approval_decided",
                stage=row.get("step") or "approval",
                chunk_number=chunk_number,
                level="error" if row.get("status") == "rejected" else "info",
                category=_approval_gate_category(row),
                title="Approval gate decided",
                detail="Human approval decision persisted.",
                data={
                    "approval_gate_id": gate_id,
                    "step": row.get("step"),
                    "status": row.get("status"),
                    "approval_type": row.get("approval_type"),
                },
            )


def _add_chunk_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, risk_level, requires_human_review, status,
                   test_run_verdict, started_at, completed_at, created_at
            FROM chunks
            WHERE run_id = :run_id
            ORDER BY created_at, chunk_number, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        chunk_number = _int_or_none(row.get("chunk_number"))
        chunk_id = row.get("id")
        data = {
            "chunk_id": chunk_id,
            "status": row.get("status"),
            "risk_level": row.get("risk_level"),
            "requires_human_review": _bool_or_none(row.get("requires_human_review")),
            "test_run_verdict": row.get("test_run_verdict"),
        }
        _append(
            entries,
            entry_id=f"chunk:{chunk_number}:{chunk_id}:created",
            ts=row.get("created_at"),
            kind="chunk_created",
            stage="execution",
            chunk_number=chunk_number,
            level="info",
            category="execution",
            title=f"Chunk {chunk_number} created",
            detail="Chunk persisted.",
            data=data,
        )
        if row.get("started_at"):
            _append(
                entries,
                entry_id=f"chunk:{chunk_number}:{chunk_id}:started",
                ts=row.get("started_at"),
                kind="chunk_started",
                stage="execution",
                chunk_number=chunk_number,
                level="info",
                category="execution",
                title=f"Chunk {chunk_number} started",
                detail="Persisted chunk start timestamp recorded.",
                data=data,
            )
        if row.get("completed_at"):
            status = _safe_str(row.get("status")) or "unknown"
            _append(
                entries,
                entry_id=f"chunk:{chunk_number}:{chunk_id}:completed",
                ts=row.get("completed_at"),
                kind="chunk_completed",
                stage="execution",
                chunk_number=chunk_number,
                level="error" if "fail" in status else "info",
                category="execution",
                title=f"Chunk {chunk_number} completed",
                detail=f"Persisted chunk status is {status}.",
                data=data,
            )


def _add_chunk_attempt_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, attempt_number, entry_mode, final_outcome_class,
                   final_status, stage_profile, trivial_profile_eligible, created_at
            FROM chunk_attempts
            WHERE run_id = :run_id
            ORDER BY created_at, chunk_number, attempt_number, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        chunk_number = _int_or_none(row.get("chunk_number"))
        attempt_number = row.get("attempt_number")
        _append(
            entries,
            entry_id=f"chunk_attempt:{chunk_number}:{attempt_number}:{row.get('id')}",
            ts=row.get("created_at"),
            kind="stage_completed",
            stage="execution",
            chunk_number=chunk_number,
            level="info",
            category="execution",
            title=f"Chunk {chunk_number} attempt {attempt_number} recorded",
            detail="Chunk attempt outcome persisted.",
            data={
                "chunk_attempt_id": row.get("id"),
                "attempt_number": attempt_number,
                "entry_mode": row.get("entry_mode"),
                "final_outcome_class": row.get("final_outcome_class"),
                "final_status": row.get("final_status"),
                "stage_profile": row.get("stage_profile"),
                "trivial_profile_eligible": _bool_or_none(row.get("trivial_profile_eligible")),
            },
        )


def _add_scope_expansion_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, status, requested_files, approved_files,
                   decided_at, applied_at, created_at
            FROM scope_expansion_requests
            WHERE run_id = :run_id
            ORDER BY created_at, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        request_id = row.get("id")
        chunk_number = _int_or_none(row.get("chunk_number"))
        data = {
            "scope_expansion_request_id": request_id,
            "status": row.get("status"),
            "requested_files_count": _jsonish_count(row.get("requested_files")),
            "approved_files_count": _jsonish_count(row.get("approved_files")),
            "applied": bool(row.get("applied_at")),
        }
        _append(
            entries,
            entry_id=f"scope_expansion:{request_id}:created",
            ts=row.get("created_at"),
            kind="scope_expansion_requested",
            stage="scope_expansion",
            chunk_number=chunk_number,
            level="warn" if row.get("status") == "pending" else "info",
            category="execution",
            title="Scope expansion requested",
            detail="Scope expansion request persisted.",
            data=data,
        )
        if row.get("decided_at"):
            _append(
                entries,
                entry_id=f"scope_expansion:{request_id}:decided",
                ts=row.get("decided_at"),
                kind="scope_expansion_decided",
                stage="scope_expansion",
                chunk_number=chunk_number,
                level="error" if row.get("status") == "rejected" else "info",
                category="execution",
                title="Scope expansion decided",
                detail="Scope expansion decision persisted.",
                data=data,
            )


def _add_test_ack_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, verdict, status, acknowledged_at, created_at
            FROM test_validation_acknowledgements
            WHERE run_id = :run_id
            ORDER BY created_at, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        chunk_number = _int_or_none(row.get("chunk_number"))
        _append(
            entries,
            entry_id=f"test_validation_ack:{row.get('id')}",
            ts=row.get("acknowledged_at") or row.get("created_at"),
            kind="acknowledgement_recorded",
            stage="test_validation",
            chunk_number=chunk_number,
            level="info",
            category="review",
            title="Test validation acknowledged",
            detail="Test validation acknowledgement persisted.",
            data={
                "test_validation_acknowledgement_id": row.get("id"),
                "verdict": row.get("verdict"),
                "status": row.get("status"),
            },
        )


def _add_review_ack_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, status, acknowledged_at, created_at
            FROM review_finding_acknowledgements
            WHERE run_id = :run_id
            ORDER BY created_at, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        chunk_number = _int_or_none(row.get("chunk_number"))
        _append(
            entries,
            entry_id=f"review_finding_ack:{row.get('id')}",
            ts=row.get("acknowledged_at") or row.get("created_at"),
            kind="acknowledgement_recorded",
            stage="review",
            chunk_number=chunk_number,
            level="info",
            category="review",
            title="Review finding acknowledged",
            detail="Review finding acknowledgement persisted.",
            data={
                "review_finding_acknowledgement_id": row.get("id"),
                "status": row.get("status"),
            },
        )


def _add_chunk_review_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, review_status, verdict, created_at
            FROM chunk_reviews
            WHERE run_id = :run_id
            ORDER BY created_at, chunk_number, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        chunk_number = _int_or_none(row.get("chunk_number"))
        _append(
            entries,
            entry_id=f"chunk_review:{chunk_number}:{row.get('id')}",
            ts=row.get("created_at"),
            kind="review_recorded",
            stage="review",
            chunk_number=chunk_number,
            level="warn" if row.get("verdict") == "needs_changes" else "info",
            category="review",
            title=f"Chunk {chunk_number} review recorded",
            detail="Chunk review persisted.",
            data={
                "chunk_review_id": row.get("id"),
                "review_status": row.get("review_status"),
                "verdict": row.get("verdict"),
            },
        )


def _add_memory_injection_entries(entries: list[TimelineEntry], conn: Any, run_id: str) -> None:
    rows = conn.execute(
        text(
            """
            SELECT id, chunk_number, role, attempt_number, token_budget,
                   included_count, excluded_count, created_at
            FROM memory_injection_events
            WHERE run_id = :run_id
            ORDER BY created_at, id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        chunk_number = _int_or_none(row.get("chunk_number"))
        _append(
            entries,
            entry_id=f"memory_injection:{row.get('id')}",
            ts=row.get("created_at"),
            kind="memory_injected",
            stage="memory",
            chunk_number=chunk_number,
            level="info",
            category="memory",
            title="Memory injection recorded",
            detail="Memory injection provenance persisted.",
            data={
                "memory_injection_event_id": row.get("id"),
                "role": row.get("role"),
                "attempt_number": row.get("attempt_number"),
                "token_budget": row.get("token_budget"),
                "included_count": row.get("included_count"),
                "excluded_count": row.get("excluded_count"),
            },
        )


def _append(
    entries: list[TimelineEntry],
    *,
    entry_id: str,
    ts: Any,
    kind: str,
    stage: Any,
    chunk_number: int | None,
    level: str,
    category: str,
    title: str,
    detail: str | None,
    data: dict[str, Any],
) -> None:
    entries.append(
        TimelineEntry(
            id=entry_id,
            ts=_coerce_ts(ts),
            kind=kind,
            stage=_sanitize_text(stage) if stage is not None else None,
            chunk_number=chunk_number,
            level=level,
            category=category,
            title=_sanitize_text(title),
            detail=_sanitize_text(detail) if detail is not None else None,
            data=_sanitize_payload(data),
        )
    )


def _entry_sort_key(entry: TimelineEntry) -> tuple[str, int, int, str, str]:
    return (
        entry.ts,
        CATEGORY_ORDER.get(entry.category, 999),
        KIND_ORDER.get(entry.kind, 999),
        entry.kind,
        entry.id,
    )


def _approval_gate_category(row: dict[str, Any]) -> str:
    approval_type = _safe_str(row.get("approval_type")) or ""
    step = _safe_str(row.get("step")) or ""
    approval_type = approval_type.lower()
    step = step.lower()
    if approval_type == "final" or step == "final-approval":
        return "ship"
    if approval_type == "memory_conflict":
        return "memory"
    return "approval"


def _coerce_ts(value: Any) -> str:
    if value is None:
        return "1970-01-01T00:00:00+00:00"
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _sanitize_text(str(key)): _sanitize_payload(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text(str(value))


def _sanitize_text(value: Any) -> str:
    text_value = str(value)
    lowered = text_value.lower()
    if any(marker in lowered for marker in _TECHNICAL_PAYLOAD_MARKERS):
        return "[redacted technical payload]"
    for pattern in _SECRET_PATTERNS:
        text_value = pattern.sub("[redacted]", text_value)
    if len(text_value) > 300:
        return f"{text_value[:297]}..."
    return text_value


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _jsonish_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    text_value = str(value).strip()
    if not text_value:
        return 0
    if text_value[0] in "[{":
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (list, dict)):
            return len(parsed)
    parts = [part for part in text_value.split(",") if part.strip()]
    return len(parts) if parts else 0
