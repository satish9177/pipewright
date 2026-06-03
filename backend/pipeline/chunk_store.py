"""
chunk_store.py
Persistence helpers for Phase 2B chunk plans.

This module stores and reads chunk plans. It does not execute chunks.
"""

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from backend.core.status_service import update_chunk_status as _service_update_chunk_status
from backend.core.statuses import ChunkPlanStatus, ChunkStatusValue, RunStatus
from backend.db.database import engine, init_db
from backend.models.chunk import (
    ChunkPlanResponse,
    ChunkStatus,
    PendingScopeExpansion,
    TestRunValidation,
    TriageResult,
)
from backend.pipeline.scope_expansion import (
    ScopeExpansionStatus,
    compute_effective_files_expected,
    is_in_force,
)
from backend.pipeline.test_run_validation import TestRunValidationResult


def _json_loads_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _json_loads_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _nullable_bool(value) -> bool | None:
    """Map a nullable 0/1 SQLite int back to bool, preserving NULL as None."""
    if value is None:
        return None
    return bool(value)


_VALID_TEST_RUN_VERDICTS = {"strong", "weak", "none", "unknown"}


def _test_validation_from_row(data: dict) -> TestRunValidation | None:
    """
    Build the read-only runtime test-validation view (#28E) from a chunk row's
    persisted ``test_run_*`` columns (#28D), or ``None`` when no verdict was
    recorded.

    Display-only and fail-safe: an unrecognized/corrupt verdict value yields
    ``None`` rather than raising, so surfacing evidence can never 500 the chunk
    plan response.
    """
    verdict = data.get("test_run_verdict")
    if verdict is None or verdict not in _VALID_TEST_RUN_VERDICTS:
        return None
    counts = _json_loads_dict(data.get("test_run_counts_json"))
    return TestRunValidation(
        verdict=verdict,
        reason=data.get("test_run_verdict_reason") or "",
        command_quality=data.get("test_run_command_quality"),
        counts_parsed=bool(data.get("test_run_counts_parsed")),
        total_tests=counts.get("total"),
        passed_tests=counts.get("passed"),
        failed_tests=counts.get("failed"),
        zero_tests_detected=bool(data.get("test_run_zero_tests_detected")),
    )


def _chunk_row_to_status(row) -> ChunkStatus:
    data = dict(row._mapping)
    return ChunkStatus(
        run_id=data["run_id"],
        project_id=data["project_id"],
        chunk_number=data["chunk_number"],
        title=data["title"],
        status=data["status"],
        risk_level=data["risk_level"],
        requires_human_review=bool(data["requires_human_review"]),
        files_expected=_json_loads_list(data.get("files_expected")),
        depends_on=_json_loads_list(data.get("depends_on")),
        completion_summary=data.get("completion_summary"),
        error_message=data.get("error_message"),
        test_validation=_test_validation_from_row(data),
    )


def _load_run(conn, run_id: str):
    return conn.execute(text("""
        SELECT * FROM pipeline_runs
        WHERE id = :run_id
    """), {"run_id": run_id}).fetchone()


def _load_chunk_rows(conn, run_id: str):
    return conn.execute(text("""
        SELECT * FROM chunks
        WHERE run_id = :run_id
        ORDER BY chunk_number ASC
    """), {"run_id": run_id}).fetchall()


def _load_in_force_scope_files(conn, run_id: str) -> dict[int, list[str]]:
    """
    Map chunk_number -> approved_files of in-force scope expansion requests (#27C).

    In-force membership is decided by the pure scope_expansion.is_in_force (the
    single source of truth — approved/applied), never hardcoded here, so the
    overlay can never drift from the lifecycle invariant. Rows are read oldest
    first so the merge order is deterministic when more than one in-force row
    exists for a chunk. This is read-only and never mutates chunks.files_expected.
    """
    rows = conn.execute(text("""
        SELECT chunk_number, approved_files, status
        FROM scope_expansion_requests
        WHERE run_id = :run_id
        ORDER BY created_at ASC, id ASC
    """), {"run_id": run_id}).fetchall()

    in_force: dict[int, list[str]] = {}
    for row in rows:
        data = dict(row._mapping)
        status = data.get("status")
        # Fail safe: an unknown/NULL status is treated as NOT in force (it never
        # widens scope) rather than crashing this live read path.
        try:
            if not status or not is_in_force(status):
                continue
        except ValueError:
            continue
        in_force.setdefault(data["chunk_number"], []).extend(
            _json_loads_list(data.get("approved_files"))
        )
    return in_force


def _load_pending_scope_expansion(
    conn, run_id: str
) -> dict[int, PendingScopeExpansion]:
    """
    Map chunk_number -> the single pending scope expansion request (#27F).

    Read-only surfacing for the frontend approve/reject UI. The store guarantees
    at most one pending request per chunk (older pending requests are superseded
    when a newer eligible failure arrives), so a plain dict keyed by chunk_number
    is sufficient; if more than one ever appears, the most recent (by created_at)
    wins. Only `pending` requests are surfaced — approved/applied feed the
    effective-scope overlay instead, and rejected/superseded are inert. This never
    mutates anything and never affects effective scope or execution.
    """
    rows = conn.execute(text("""
        SELECT id, chunk_number, failure_report_id, requested_files,
               status, created_at
        FROM scope_expansion_requests
        WHERE run_id = :run_id
        ORDER BY created_at ASC, id ASC
    """), {"run_id": run_id}).fetchall()

    pending: dict[int, PendingScopeExpansion] = {}
    for row in rows:
        data = dict(row._mapping)
        if data.get("status") != ScopeExpansionStatus.PENDING.value:
            continue
        pending[data["chunk_number"]] = PendingScopeExpansion(
            request_id=data["id"],
            chunk_number=data["chunk_number"],
            failure_report_id=data.get("failure_report_id") or "",
            requested_files=_json_loads_list(data.get("requested_files")),
            status=data["status"],
            created_at=data.get("created_at"),
        )
    return pending


def _insert_chunks(conn, run_id: str, project_id: str, triage_result: TriageResult) -> int:
    for chunk in triage_result.chunks:
        conn.execute(text("""
            INSERT OR IGNORE INTO chunks
            (
                id, run_id, project_id, chunk_number, title, description,
                files_expected, depends_on, risk_level, token_estimate,
                requires_human_review, status
            )
            VALUES
            (
                :id, :run_id, :project_id, :chunk_number, :title,
                :description, :files_expected, :depends_on, :risk_level,
                :token_estimate, :requires_human_review, :status
            )
        """), {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "project_id": project_id,
            "chunk_number": chunk.chunk_number,
            "title": chunk.title,
            "description": chunk.description,
            "files_expected": json.dumps(chunk.files_expected),
            "depends_on": json.dumps(chunk.depends_on),
            "risk_level": chunk.risk_level,
            "token_estimate": chunk.token_estimate,
            "requires_human_review": 1 if chunk.requires_human_review else 0,
            "status": ChunkStatusValue.PENDING,
        })

    row = conn.execute(text("""
        SELECT COUNT(*) FROM chunks
        WHERE run_id = :run_id
    """), {"run_id": run_id}).fetchone()
    return int(row[0])


def create_chunked_run(
    run_id: str,
    project_id: str,
    feature_description: str,
    triage_result: TriageResult,
    *,
    intent: str | None = None,
    source_plan_run_id: str | None = None,
) -> ChunkPlanResponse:
    """
    Create one parent pipeline run and its proposed chunks transactionally.

    ``intent`` and ``source_plan_run_id`` are optional. ``source_plan_run_id``
    is set when this run is derived from a prior plan_only run via the
    plan-to-implementation handoff, so it can be looked up for idempotency.
    """
    try:
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_runs
                (
                    id, project_id, feature_description, status,
                    current_step, intent, chunk_plan_status, chunk_plan,
                    total_chunks, current_chunk_number,
                    source_plan_run_id, created_at
                )
                VALUES
                (
                    :id, :project_id, :feature_description,
                    :status, 'chunk_plan', :intent,
                    :chunk_plan_status, :chunk_plan, :total_chunks,
                    0, :source_plan_run_id, :created_at
                )
            """), {
                "id": run_id,
                "project_id": project_id,
                "feature_description": feature_description,
                "status": RunStatus.AWAITING_CHUNK_PLAN_APPROVAL,
                "intent": intent or "implementation",
                "chunk_plan_status": ChunkPlanStatus.AWAITING_APPROVAL,
                "chunk_plan": triage_result.model_dump_json(),
                "total_chunks": triage_result.total_chunks,
                "source_plan_run_id": source_plan_run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            _insert_chunks(conn, run_id, project_id, triage_result)

        print(f"[CHUNKS] Chunked run created | run_id={run_id}")
        return get_chunk_plan_status(run_id)
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: create_chunked_run failed. "
            f"run_id={run_id} | error={error}"
        )


def save_chunks_to_db(
    run_id: str,
    project_id: str,
    triage_result: TriageResult,
) -> int:
    """
    Save chunk rows idempotently for an existing run.
    """
    try:
        init_db()
        with engine.begin() as conn:
            count = _insert_chunks(conn, run_id, project_id, triage_result)
        print(f"[CHUNKS] Chunks saved | run_id={run_id} | count={count}")
        return count
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: save_chunks_to_db failed. "
            f"run_id={run_id} | error={error}"
        )


def get_chunk_plan_status(run_id: str) -> ChunkPlanResponse:
    try:
        init_db()
        with engine.connect() as conn:
            run = _load_run(conn, run_id)
            if run is None:
                raise ValueError(f"chunk_store.py: run not found: {run_id}")
            # Effective-scope overlay (#27C), computed in exactly ONE site. The
            # original chunks.files_expected stays immutable; effective scope is
            # original UNION approved_files of in-force scope expansion requests.
            # The `if extras` guard keeps output byte-identical to pre-#27C when
            # no in-force request exists (the common case until requests are
            # wired in #27E), so this overlay does not perturb the live pipeline.
            in_force_scope = _load_in_force_scope_files(conn, run_id)
            pending_scope = _load_pending_scope_expansion(conn, run_id)
            chunks = []
            for row in _load_chunk_rows(conn, run_id):
                chunk_status = _chunk_row_to_status(row)
                updates: dict = {}
                extras = in_force_scope.get(chunk_status.chunk_number)
                if extras:
                    updates["files_expected"] = compute_effective_files_expected(
                        chunk_status.files_expected, extras
                    )
                pending = pending_scope.get(chunk_status.chunk_number)
                if pending is not None:
                    # Read-only overlay (#27F): never affects effective scope.
                    updates["pending_scope_expansion"] = pending
                if updates:
                    chunk_status = chunk_status.model_copy(update=updates)
                chunks.append(chunk_status)

        run_data = dict(run._mapping)
        triage = None
        if run_data.get("chunk_plan"):
            triage = TriageResult.model_validate_json(run_data["chunk_plan"])

        return ChunkPlanResponse(
            run_id=run_id,
            project_id=run_data.get("project_id") or "",
            chunk_plan_status=run_data.get("chunk_plan_status") or ChunkPlanStatus.NONE,
            total_chunks=run_data.get("total_chunks") or len(chunks),
            current_chunk_number=run_data.get("current_chunk_number") or 0,
            triage=triage,
            chunks=chunks,
        )
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_chunk_plan_status failed. "
            f"run_id={run_id} | error={error}"
        )


def _require_awaiting_approval(conn, run_id: str):
    run = _load_run(conn, run_id)
    if run is None:
        raise ValueError(f"chunk_store.py: run not found: {run_id}")

    status = dict(run._mapping).get("chunk_plan_status")
    if status != ChunkPlanStatus.AWAITING_APPROVAL:
        raise RuntimeError(
            f"chunk_store.py: chunk plan is not awaiting approval. "
            f"run_id={run_id} | status={status}"
        )
    return run


def approve_chunk_plan(run_id: str) -> ChunkPlanResponse:
    try:
        init_db()
        with engine.begin() as conn:
            _require_awaiting_approval(conn, run_id)
            conn.execute(text("""
                UPDATE pipeline_runs
                SET chunk_plan_status = :chunk_plan_status,
                    status = :status,
                    current_step = :current_step
                WHERE id = :run_id
            """), {
                "run_id": run_id,
                "chunk_plan_status": ChunkPlanStatus.APPROVED,
                "status": RunStatus.CHUNK_PLAN_APPROVED,
                "current_step": RunStatus.CHUNK_PLAN_APPROVED,
            })
        print(f"[CHUNKS] Chunk plan approved | run_id={run_id}")
        return get_chunk_plan_status(run_id)
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: approve_chunk_plan failed. "
            f"run_id={run_id} | error={error}"
        )


def reject_chunk_plan(
    run_id: str,
    reason: str | None = None,
) -> ChunkPlanResponse:
    try:
        init_db()
        with engine.begin() as conn:
            _require_awaiting_approval(conn, run_id)
            conn.execute(text("""
                UPDATE pipeline_runs
                SET chunk_plan_status = :chunk_plan_status,
                    status = :status,
                    current_step = 'chunk_plan_rejected',
                    plain_english_summary = :reason
                WHERE id = :run_id
            """), {
                "run_id": run_id,
                "chunk_plan_status": ChunkPlanStatus.REJECTED,
                "status": RunStatus.REJECTED,
                "reason": reason or "Chunk plan rejected.",
            })
        print(f"[CHUNKS] Chunk plan rejected | run_id={run_id}")
        return get_chunk_plan_status(run_id)
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: reject_chunk_plan failed. "
            f"run_id={run_id} | error={error}"
        )


def get_pending_chunks(run_id: str) -> list[ChunkStatus]:
    try:
        init_db()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM chunks
                WHERE run_id = :run_id AND status = :status
                ORDER BY chunk_number ASC
            """), {
                "run_id": run_id,
                "status": ChunkStatusValue.PENDING,
            }).fetchall()
        return [_chunk_row_to_status(row) for row in rows]
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_pending_chunks failed. "
            f"run_id={run_id} | error={error}"
        )


def get_chunk(run_id: str, chunk_number: int) -> ChunkStatus:
    try:
        init_db()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM chunks
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            }).fetchone()
        if row is None:
            raise ValueError(
                f"chunk_store.py: chunk not found. "
                f"run_id={run_id} | chunk_number={chunk_number}"
            )
        return _chunk_row_to_status(row)
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_chunk failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def update_chunk_status(
    run_id: str,
    chunk_number: int,
    status: str,
    error_message: str | None = None,
) -> None:
    _service_update_chunk_status(run_id, chunk_number, status, error_message)


def save_chunk_completion_summary(
    run_id: str,
    chunk_number: int,
    summary: dict | str,
) -> None:
    try:
        summary_text = json.dumps(summary) if isinstance(summary, dict) else summary
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE chunks
                SET completion_summary = :summary,
                    completed_at = :completed_at
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "summary": summary_text,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "chunk_number": chunk_number,
            })
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: save_chunk_completion_summary failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def save_chunk_test_run_verdict(
    run_id: str,
    chunk_number: int,
    result: TestRunValidationResult,
) -> None:
    """
    Persist the display-only runtime test-validation verdict on the chunk (#28D).

    This is purely additive evidence: it writes only the dedicated
    ``test_run_*`` columns and never touches ``status``, ``completion_summary``,
    approval gates, or any run/chunk outcome. It does not gate, block, commit, or
    roll anything back. Surfacing (#28E) and enforcement (#28F) are later slices.

    Counts are stored as a small JSON blob ({total,passed,failed}) mirroring the
    pure classifier's parsed evidence; ``None`` counts are preserved as JSON null,
    so "absence of a count" is never confused with "zero tests".
    """
    try:
        counts_json = json.dumps({
            "total": result.total_tests,
            "passed": result.passed_tests,
            "failed": result.failed_tests,
        })
        init_db()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE chunks
                SET test_run_verdict = :verdict,
                    test_run_verdict_reason = :reason,
                    test_run_command_quality = :command_quality,
                    test_run_counts_parsed = :counts_parsed,
                    test_run_zero_tests_detected = :zero_tests_detected,
                    test_run_counts_json = :counts_json
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "verdict": result.verdict.value,
                "reason": result.reason,
                "command_quality": result.command_quality,
                "counts_parsed": 1 if result.counts_parsed else 0,
                "zero_tests_detected": 1 if result.zero_tests_detected else 0,
                "counts_json": counts_json,
                "run_id": run_id,
                "chunk_number": chunk_number,
            })
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: save_chunk_test_run_verdict failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def get_chunk_test_run_verdict(
    run_id: str,
    chunk_number: int,
) -> dict | None:
    """
    Read back the display-only runtime test verdict for a chunk (#28D).

    Returns ``None`` when the chunk does not exist or has no recorded verdict
    (e.g. a pre-#28D chunk, or one skip-completed from a checkpoint without a
    fresh test run). Read-only; surfaces evidence for tests and later #28E.
    """
    try:
        init_db()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT test_run_verdict, test_run_verdict_reason,
                       test_run_command_quality, test_run_counts_parsed,
                       test_run_zero_tests_detected, test_run_counts_json
                FROM chunks
                WHERE run_id = :run_id AND chunk_number = :chunk_number
            """), {
                "run_id": run_id,
                "chunk_number": chunk_number,
            }).fetchone()
        if row is None:
            return None
        data = dict(row._mapping)
        if data.get("test_run_verdict") is None:
            return None
        counts = _json_loads_dict(data.get("test_run_counts_json"))
        return {
            "verdict": data["test_run_verdict"],
            "reason": data.get("test_run_verdict_reason"),
            "command_quality": data.get("test_run_command_quality"),
            "counts_parsed": _nullable_bool(data.get("test_run_counts_parsed")),
            "zero_tests_detected": _nullable_bool(
                data.get("test_run_zero_tests_detected")
            ),
            "total_tests": counts.get("total"),
            "passed_tests": counts.get("passed"),
            "failed_tests": counts.get("failed"),
        }
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_chunk_test_run_verdict failed. "
            f"run_id={run_id} | chunk_number={chunk_number} | error={error}"
        )


def _format_summary_field(summary: dict, key: str) -> str:
    value = summary.get(key)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    if value:
        return str(value)
    return "none"


def _format_completed_chunk(row) -> str:
    data = dict(row._mapping)
    raw_summary = data.get("completion_summary")

    if raw_summary is None:
        return (
            f"Chunk {data['chunk_number']} - {data['title']} [COMPLETE]\n"
            f"Files created: none\n"
            f"Files modified: none\n"
            f"Key decisions: none\n"
            f"Tests added: none\n"
            f"Summary: not available (crash recovery)"
        )

    try:
        parsed = json.loads(raw_summary)
        if isinstance(parsed, dict):
            return (
                f"Chunk {data['chunk_number']} - {data['title']} [COMPLETE]\n"
                f"Files created: {_format_summary_field(parsed, 'files_created')}\n"
                f"Files modified: {_format_summary_field(parsed, 'files_modified')}\n"
                f"Key decisions: {_format_summary_field(parsed, 'key_decisions')}\n"
                f"Tests added: {_format_summary_field(parsed, 'tests_added')}\n"
                f"Summary: {_format_summary_field(parsed, 'summary')}"
            )
    except Exception:
        pass

    return (
        f"Chunk {data['chunk_number']} - {data['title']} [COMPLETE]\n"
        f"Files created: none\n"
        f"Files modified: none\n"
        f"Key decisions: none\n"
        f"Tests added: none\n"
        f"Summary: {raw_summary}"
    )


def get_previous_chunks_context(run_id: str, before_chunk_number: int) -> str:
    try:
        init_db()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT * FROM chunks
                WHERE run_id = :run_id
                  AND chunk_number < :before_chunk_number
                  AND status = :status
                ORDER BY chunk_number ASC
            """), {
                "run_id": run_id,
                "before_chunk_number": before_chunk_number,
                "status": ChunkStatusValue.COMPLETED,
            }).fetchall()

        parts = ["[Previous Chunks Context]"]
        for row in rows:
            parts.append("")
            parts.append(_format_completed_chunk(row))
        parts.append("")
        parts.append("[End Previous Context]")
        context = "\n".join(parts)
        return context[:6000]
    except Exception as error:
        raise RuntimeError(
            f"chunk_store.py: get_previous_chunks_context failed. "
            f"run_id={run_id} | before_chunk_number={before_chunk_number} | "
            f"error={error}"
        )
