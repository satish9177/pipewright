"""
scope_expansion_store.py
Persistence helpers for Scope Expansion Recovery requests (#27C).

This module stores and reads scope_expansion_requests rows. It is the durable,
audited home for human-approved scope amendments described in
docs/design/scope-expansion-recovery.md (section 5/section 6/section 8).

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

from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.pipeline.patch_failures import PatchFailureReport
from backend.pipeline.scope_expansion import (
    ScopeExpansionRequest,
    ScopeExpansionStatus,
    evaluate_scope_expansion_eligibility,
    is_in_force,
    is_transition_allowed,
    normalize_safe_relative_path,
)


class ScopeExpansionConflictError(Exception):
    """
    Raised when a scope expansion request exists and belongs to the given
    run/chunk but cannot be acted on in its current state (e.g. a reject of a
    request that is not pending). Distinct from ValueError (not found) so the
    route layer can map it to HTTP 409 rather than 404. Carries no mutation: the
    caller has changed nothing when this is raised.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEFAULT_REJECTION_REASON = "Scope expansion rejected."


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


def reject_scope_expansion_request(
    run_id: str,
    chunk_number: int,
    request_id: str,
    *,
    reason: str | None = None,
    decided_by: str | None = None,
) -> ScopeExpansionRequest:
    """
    Reject a pending scope expansion request (pending -> rejected).

    Rejecting is NOT retry and NOT code approval. It records the human's decision
    and nothing else: it never creates approved/applied rows, never touches
    approved_files, never runs a retry, never commits, and never mutates
    chunks.files_expected. After rejection the request is not in force, so
    effective scope reverts to the original files_expected.

    Safety / validation (mutates nothing on any failure):
      - request not found                          -> ValueError (route -> 404)
      - request belongs to a different run/chunk   -> ValueError (route -> 404)
      - request is not pending                     -> ScopeExpansionConflictError
                                                      (route -> 409); approved/
                                                      applied/rejected/superseded
                                                      requests are left untouched,
                                                      so a double reject is a 409.

    The decision reason defaults to a safe placeholder when blank so the audit
    column is always populated (mirrors reject_chunk_plan). decided_at is stamped
    by the underlying status transition.
    """
    request = get_scope_expansion_request(request_id)  # ValueError if missing.
    if request.run_id != run_id or request.chunk_number != chunk_number:
        raise ValueError(
            "scope_expansion_store.py: scope expansion request "
            f"{request_id} not found for run {run_id} chunk {chunk_number}"
        )
    if request.status != ScopeExpansionStatus.PENDING.value:
        raise ScopeExpansionConflictError(
            "scope_expansion_store.py: scope expansion request "
            f"{request_id} is not pending (status={request.status}); nothing changed"
        )

    resolved_reason = reason if (reason and reason.strip()) else _DEFAULT_REJECTION_REASON
    return update_scope_expansion_request_status(
        request_id,
        ScopeExpansionStatus.REJECTED,
        decided_by=decided_by,
        decision_reason=resolved_reason,
    )


def count_in_force_scope_amendments(run_id: str, chunk_number: int) -> int:
    """
    Count in-force (approved/applied) scope amendments for a chunk.

    This is ``amendments_used`` for the #27B eligibility gate. In-force membership
    is decided only by the pure scope_expansion.is_in_force, so the count can never
    drift from the lifecycle invariant. pending/rejected/superseded never count.
    """
    requests = list_scope_expansion_requests_for_chunk(run_id, chunk_number)
    return sum(1 for request in requests if is_in_force(request.status))


# ---------------------------------------------------------------------------
# Request creation on an eligible clean SCOPE_VIOLATION
# ---------------------------------------------------------------------------


class ScopeExpansionCreationResult(BaseModel):
    """
    Small result of maybe_create_scope_expansion_request_for_failure (for tests
    and logging). ``request`` is the active pending request when one now exists
    for the chunk (whether freshly created or an idempotent re-hit of the same
    failure), and None when the failure was ineligible and nothing was created.
    """

    created: bool = False
    eligible: bool = False
    reason: str | None = None
    request: ScopeExpansionRequest | None = None
    superseded_request_ids: list[str] = Field(default_factory=list)


def _extra_files_outside_scope(
    attempted: Sequence[str],
    actual: Sequence[str],
    allowed: Sequence[str],
) -> list[str]:
    """
    The paths a failed attempt tried to touch that are outside the approved scope.

    Pure. Compares safe-normalized forms so case/slash variants of an in-scope
    file are not surfaced as "extra". Returns the original path strings (order
    preserved, first occurrence wins); denylist filtering and final normalization
    are applied later by filter_requestable_files via the eligibility helper.
    """
    allowed_normalized = {
        normalized
        for raw in allowed
        if (normalized := normalize_safe_relative_path(raw)) is not None
    }
    extras: list[str] = []
    seen: set[str] = set()
    for raw in list(attempted) + list(actual):
        normalized = normalize_safe_relative_path(raw)
        if normalized is None:
            # Structurally unsafe; let the eligibility filter drop it, but still
            # consider it a candidate so an all-unsafe set yields no requestables.
            key = raw
        elif normalized in allowed_normalized:
            continue
        else:
            key = normalized
        if key in seen:
            continue
        seen.add(key)
        extras.append(raw)
    return extras


def maybe_create_scope_expansion_request_for_failure(
    run_id: str,
    project_id: str,
    chunk_number: int,
    report: PatchFailureReport,
) -> ScopeExpansionCreationResult:
    """
    Create a pending scope_expansion_request for an eligible clean SCOPE_VIOLATION
    (#27 — request creation slice).

    This ONLY creates/surfaces a request. It does NOT approve, retry, commit, or
    mutate chunks.files_expected, and it never touches scope_guard. A request is
    created only when scope_expansion.evaluate_scope_expansion_eligibility says the
    failure is eligible — i.e. failure_type == SCOPE_VIOLATION, working tree clean,
    no manual intervention needed, at least one requestable (normalized,
    non-forbidden) extra file, and MAX_SCOPE_AMENDMENTS not exhausted. Dirty-tree /
    manual-intervention / non-SCOPE_VIOLATION / all-forbidden / cap-exhausted
    failures create nothing.

    Idempotency / dedup (#27A section 6, #27C):
      - A failure with no failure_report_id creates nothing (cannot be tied).
      - If a pending request already exists for this chunk with the SAME
        failure_report_id, it is left unchanged (no duplicate).
      - If pending requests exist for OTHER failure_report_ids, they are
        superseded and a single fresh pending request is created, so there is
        never more than one active pending request per chunk.

    Pure-ish persistence: no git, no LLM, no commit. Returns a small result for
    tests/logging. requested_files come from the report's attempted/actual changed
    files outside the approved scope; approved_files is always empty.
    """
    if not report.failure_report_id:
        return ScopeExpansionCreationResult(
            created=False, eligible=False, reason="missing_failure_report_id"
        )

    candidate_extras = _extra_files_outside_scope(
        report.changed_files_attempted,
        report.changed_files_actual,
        report.allowed_files,
    )

    decision = evaluate_scope_expansion_eligibility(
        failure_type=report.failure_type,
        working_tree_clean=report.working_tree_clean,
        manual_intervention_needed=report.manual_intervention_needed,
        amendments_used=count_in_force_scope_amendments(run_id, chunk_number),
        requested_extra_files=candidate_extras,
    )
    if not decision.eligible:
        return ScopeExpansionCreationResult(
            created=False, eligible=False, reason=decision.reason
        )

    existing = list_scope_expansion_requests_for_chunk(run_id, chunk_number)
    pending = [
        request
        for request in existing
        if request.status == ScopeExpansionStatus.PENDING.value
    ]

    # Idempotent: the same failure already has a pending request -> do nothing.
    for request in pending:
        if request.failure_report_id == report.failure_report_id:
            return ScopeExpansionCreationResult(
                created=False,
                eligible=True,
                reason="already_pending_same_failure",
                request=request,
            )

    # A newer eligible failure replaces any stale pending request(s) for the chunk
    # so there is never more than one active pending request.
    superseded_ids: list[str] = []
    for request in pending:
        update_scope_expansion_request_status(
            request.id,
            ScopeExpansionStatus.SUPERSEDED,
            decision_reason="superseded by a newer scope violation",
        )
        superseded_ids.append(request.id)

    created = create_scope_expansion_request(
        run_id,
        project_id,
        chunk_number,
        report.failure_report_id,
        requested_files=decision.requestable_files,
        approved_files=[],
    )
    return ScopeExpansionCreationResult(
        created=True,
        eligible=True,
        reason="created",
        request=created,
        superseded_request_ids=superseded_ids,
    )
