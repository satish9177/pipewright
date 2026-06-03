"""
scope_expansion_store.py
Persistence helpers for Scope Expansion Recovery requests (#27C).

This module stores and reads scope_expansion_requests rows. It is the durable,
audited home for human-approved scope amendments described in
docs/design/scope-expansion-recovery.md (§5/§6/§8).

Scope of this slice (#27C foundations):

  - Boring CRUD over the scope_expansion_requests table.
  - The status state machine is enforced on update via the pure
    scope_expansion.is_transition_allowed.
  - A read helper exposes the in-force (approved/applied) approved_files that
    feed the effective-scope overlay in chunk_store.get_chunk_plan_status.

It does NOT add routes, does NOT create requests during real SCOPE_VIOLATION
failures, does NOT run retries, and does NOT weaken scope_guard. Those land in
later #27 slices.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline.scope_expansion import (
    ScopeExpansionRequest,
    ScopeExpansionStatus,
    is_in_force,
    is_transition_allowed,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_value(status: ScopeExpansionStatus | str) -> str:
    return status.value if isinstance(status, ScopeExpansionStatus) else str(status)


def _json_dumps_list(values: Sequence[str] | None) -> str:
    return json.dumps(list(values or []))


def _json_loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _row_to_request(row) -> ScopeExpansionRequest:
    data = dict(row._mapping)
    return ScopeExpansionRequest(
        id=data["id"],
        run_id=data["run_id"],
        project_id=data["project_id"],
        chunk_number=data["chunk_number"],
        failure_report_id=data["failure_report_id"],
        requested_files=_json_loads_list(data.get("requested_files")),
        approved_files=_json_loads_list(data.get("approved_files")),
        status=data.get("status") or ScopeExpansionStatus.PENDING.value,
        created_at=data.get("created_at"),
        decided_at=data.get("decided_at"),
        applied_at=data.get("applied_at"),
        decided_by=data.get("decided_by"),
        decision_reason=data.get("decision_reason"),
    )


def create_scope_expansion_request(
    run_id: str,
    project_id: str,
    chunk_number: int,
    failure_report_id: str,
    requested_files: Sequence[str] | None = None,
    approved_files: Sequence[str] | None = None,
    *,
    request_id: str | None = None,
) -> ScopeExpansionRequest:
    """
    Persist a new scope expansion request in the ``pending`` state.

    This is pure persistence: it does NOT validate approved_files or evaluate
    eligibility (that is the route layer in a later slice). ``approved_files`` is
    normally empty until a human approves; it is accepted here only so tests and
    future callers can seed records. ``requested_files`` is untrusted/diagnostic.
    """
    new_id = request_id or str(uuid.uuid4())
    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO scope_expansion_requests
                (
                    id, run_id, project_id, chunk_number, failure_report_id,
                    requested_files, approved_files, status, created_at
                )
                VALUES
                (
                    :id, :run_id, :project_id, :chunk_number, :failure_report_id,
                    :requested_files, :approved_files, :status, :created_at
                )
            """), {
                "id": new_id,
                "run_id": run_id,
                "project_id": project_id,
                "chunk_number": chunk_number,
                "failure_report_id": failure_report_id,
                "requested_files": _json_dumps_list(requested_files),
                "approved_files": _json_dumps_list(approved_files),
                "status": ScopeExpansionStatus.PENDING.value,
                "created_at": _now_iso(),
            })
        return get_scope_expansion_request(new_id)
    except Exception as error:
        raise RuntimeError(
            f"scope_expansion_store.py: create_scope_expansion_request failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def get_scope_expansion_request(request_id: str) -> ScopeExpansionRequest:
    try:
        init_db()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM scope_expansion_requests
                WHERE id = :id
            """), {"id": request_id}).fetchone()
        if row is None:
            raise ValueError(
                f"scope_expansion_store.py: scope expansion request not found: {request_id}"
            )
        return _row_to_request(row)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"scope_expansion_store.py: get_scope_expansion_request failed. "
            f"request_id={request_id} | error={error}"
        )


def list_scope_expansion_requests_for_chunk(
    run_id: str,
    chunk_number: int,
) -> list[ScopeExpansionRequest]:
    """All scope expansion requests for a chunk, oldest first (audit order)."""
    try:
        init_db()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM scope_expansion_requests
                WHERE run_id = :run_id AND chunk_number = :chunk_number
                ORDER BY created_at ASC, id ASC
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            }).fetchall()
        return [_row_to_request(row) for row in rows]
    except Exception as error:
        raise RuntimeError(
            f"scope_expansion_store.py: list_scope_expansion_requests_for_chunk failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def list_in_force_scope_expansion_files(
    run_id: str,
    chunk_number: int,
) -> list[str]:
    """
    Approved files of in-force (approved/applied) requests for one chunk.

    In-force membership is decided by the pure scope_expansion.is_in_force, the
    single source of truth — never hardcoded here — so this can never drift from
    the lifecycle invariant. Files are returned in deterministic order: by request
    created_at, then the order within each approved_files list. Not deduplicated
    here; the effective-scope merge dedups.
    """
    requests = list_scope_expansion_requests_for_chunk(run_id, chunk_number)
    files: list[str] = []
    for request in requests:
        if is_in_force(request.status):
            files.extend(request.approved_files)
    return files


def update_scope_expansion_request_status(
    request_id: str,
    new_status: ScopeExpansionStatus | str,
    *,
    decided_by: str | None = None,
    decision_reason: str | None = None,
    approved_files: Sequence[str] | None = None,
) -> ScopeExpansionRequest:
    """
    Transition a request to ``new_status``, enforcing the lifecycle state machine.

    Rejects (raises ValueError) any transition the pure
    scope_expansion.is_transition_allowed disallows, so the durable record can
    never enter an illegal state. Audit timestamps are stamped from the target:
    moving into ``applied`` sets applied_at; any other decision sets decided_at.
    ``approved_files`` may be supplied when approving (the human-approved
    allowlist); it is persisted as-is (validation is the route layer's job).
    """
    target = _status_value(new_status)
    try:
        init_db()
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT * FROM scope_expansion_requests
                WHERE id = :id
            """), {"id": request_id}).fetchone()
            if row is None:
                raise ValueError(
                    f"scope_expansion_store.py: scope expansion request not found: {request_id}"
                )
            current = dict(row._mapping).get("status") or ScopeExpansionStatus.PENDING.value
            if not is_transition_allowed(current, target):
                raise ValueError(
                    f"scope_expansion_store.py: illegal status transition "
                    f"{current} -> {target} for request {request_id}"
                )

            assignments = ["status = :status"]
            params: dict = {"id": request_id, "status": target}
            if target == ScopeExpansionStatus.APPLIED.value:
                assignments.append("applied_at = :applied_at")
                params["applied_at"] = _now_iso()
            else:
                assignments.append("decided_at = :decided_at")
                params["decided_at"] = _now_iso()
            if decided_by is not None:
                assignments.append("decided_by = :decided_by")
                params["decided_by"] = decided_by
            if decision_reason is not None:
                assignments.append("decision_reason = :decision_reason")
                params["decision_reason"] = decision_reason
            if approved_files is not None:
                assignments.append("approved_files = :approved_files")
                params["approved_files"] = _json_dumps_list(approved_files)

            conn.execute(
                text(
                    "UPDATE scope_expansion_requests "
                    f"SET {', '.join(assignments)} WHERE id = :id"
                ),
                params,
            )
        return get_scope_expansion_request(request_id)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"scope_expansion_store.py: update_scope_expansion_request_status failed. "
            f"request_id={request_id} | new_status={target} | error={error}"
        )
