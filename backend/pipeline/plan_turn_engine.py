"""
plan_turn_engine.py

Dormant internal engine scaffold for §23 row 7b / §19 plan-gate turns.

This module exposes no route and no frontend contract. It can produce a next
plan version only while a run is still awaiting chunk-plan approval. The live
runtime pointer remains pipeline_runs.chunk_plan; plan_versions is append-only
audit/scaffold data, not an execution authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from backend.core.statuses import ChunkPlanStatus, RunStatus
from backend.db.database import engine, init_db
from backend.models.chunk import TriageResult
from backend.pipeline import policy
from backend.pipeline.chunk_store import (
    REQUEST_FILE_CONSTRAINTS_REPORT_KEY,
    replace_pending_chunks_for_plan,
)
from backend.pipeline.file_scope_intent import (
    UserFileConstraints,
    extract_user_file_constraints,
)
from backend.pipeline.plan_postprocess import apply_plan_postprocess
from backend.pipeline.plan_version_store import (
    PLAN_VERSION_SOURCE_PLAN_TURN,
    append_plan_version,
)
from backend.pipeline.run_turn_store import (
    RUN_TURN_TARGET_PLAN,
    list_run_turns,
    record_plan_turn,
)
from backend.pipeline.triage import run_triage


class PlanTurnConflictError(RuntimeError):
    """Raised when a plan turn is no longer valid for the run state."""


class PlanTurnLimitError(RuntimeError):
    """Raised when a run has reached the policy plan-turn cap."""


@dataclass(frozen=True)
class PlanTurnResult:
    run_id: str
    project_id: str
    version: int
    turn_id: str
    triage: TriageResult


def _json_loads_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _load_run(conn, run_id: str) -> dict | None:
    row = conn.execute(text("""
        SELECT *
        FROM pipeline_runs
        WHERE id = :run_id
    """), {"run_id": run_id}).mappings().fetchone()
    return dict(row) if row is not None else None


def _is_awaiting_chunk_plan_approval(run: dict) -> bool:
    return (
        run.get("status") == RunStatus.AWAITING_CHUNK_PLAN_APPROVAL
        and run.get("chunk_plan_status") == ChunkPlanStatus.AWAITING_APPROVAL
        and bool(run.get("project_id"))
        and bool(run.get("chunk_plan"))
    )


def _require_awaiting_chunk_plan_approval(conn, run_id: str) -> dict:
    run = _load_run(conn, run_id)
    if run is None:
        raise ValueError(f"plan_turn_engine.py: run not found: {run_id}")
    if not _is_awaiting_chunk_plan_approval(run):
        raise PlanTurnConflictError(
            "plan_turn_engine.py: run is not awaiting chunk-plan approval. "
            f"run_id={run_id} | status={run.get('status')} | "
            f"chunk_plan_status={run.get('chunk_plan_status')}"
        )
    return run


def _constraints_from_run(run: dict) -> UserFileConstraints:
    report = _json_loads_dict(run.get("report_json"))
    stored = report.get(REQUEST_FILE_CONSTRAINTS_REPORT_KEY)
    if isinstance(stored, dict):
        return UserFileConstraints(
            hard_allowlist=tuple(stored.get("hard_allowlist") or ()),
            preferred_files=tuple(stored.get("preferred_files") or ()),
            forbidden_files=tuple(stored.get("forbidden_files") or ()),
            reference_only_files=tuple(stored.get("reference_only_files") or ()),
            uncertain_mentions=tuple(stored.get("uncertain_mentions") or ()),
        )
    return extract_user_file_constraints(run.get("feature_description") or "")


def _load_plan_turns_before_new_message(run_id: str) -> list[dict[str, Any]]:
    return list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)


def _check_plan_turn_cap(run_id: str, prior_plan_turns: list[dict[str, Any]]) -> None:
    if len(prior_plan_turns) >= policy.PLAN_TURN_CAP:
        raise PlanTurnLimitError(
            "plan_turn_engine.py: plan turn cap reached. "
            f"run_id={run_id} | cap={policy.PLAN_TURN_CAP}"
        )


def _next_plan_version(conn, run_id: str) -> int:
    row = conn.execute(text("""
        SELECT COALESCE(MAX(version), 0) + 1
        FROM plan_versions
        WHERE run_id = :run_id
    """), {"run_id": run_id}).fetchone()
    return int(row[0] if row is not None else 1)


def _build_plan_turn_triage_request(
    *,
    original_feature_description: str,
    current_plan_json: str,
    prior_plan_turns: list[dict[str, Any]],
    new_message: str,
) -> str:
    prior_messages = [
        f"{turn['turn_number']}. {turn['steer_text']}"
        for turn in prior_plan_turns
        if turn.get("steer_text")
    ]
    prior_block = "\n".join(prior_messages) if prior_messages else "None."
    return (
        "Revise the existing chunk plan while preserving Pipewright's safety "
        "rules. Use the original request, current plan JSON, prior plan-turn "
        "messages, and newest human message as context. Return a full replacement "
        "chunk plan, not a diff.\n\n"
        "[Original feature request]\n"
        f"{original_feature_description}\n\n"
        "[Current plan JSON]\n"
        f"{current_plan_json}\n\n"
        "[Prior plan-turn messages]\n"
        f"{prior_block}\n\n"
        "[Newest plan-turn message]\n"
        f"{new_message}"
    )


async def produce_next_plan_version(
    run_id: str,
    message: str,
) -> PlanTurnResult:
    """
    Produce the next plan version for a run still awaiting plan approval.

    No route calls this in PR-A. The final mutation re-checks the run state in
    the same transaction as the version append, live pointer update, and pending
    chunk replacement. If approval/rejection happens during triage, the computed
    plan is discarded and no vN row or chunk replacement lands.
    """
    if not message or not message.strip():
        raise ValueError("plan_turn_engine.py: plan-turn message must not be blank")

    init_db()
    with engine.connect() as conn:
        run = _require_awaiting_chunk_plan_approval(conn, run_id)

    project_id = run["project_id"]
    original_feature_description = run.get("feature_description") or ""
    current_plan_json = run["chunk_plan"]
    request_file_constraints = _constraints_from_run(run)
    prior_plan_turns = _load_plan_turns_before_new_message(run_id)
    _check_plan_turn_cap(run_id, prior_plan_turns)

    recorded_turn = record_plan_turn(
        run_id=run_id,
        project_id=project_id,
        message=message,
    )
    triage_request = _build_plan_turn_triage_request(
        original_feature_description=original_feature_description,
        current_plan_json=current_plan_json,
        prior_plan_turns=prior_plan_turns,
        new_message=recorded_turn["steer_text"],
    )

    triage_result = await run_triage(
        run_id=run_id,
        project_id=project_id,
        feature_description=triage_request,
    )
    triage_result = triage_result.model_copy(update={
        "run_id": run_id,
        "project_id": project_id,
        "feature_description": original_feature_description,
    })
    triage_result = apply_plan_postprocess(
        project_id=project_id,
        triage_result=triage_result,
        request_file_constraints=request_file_constraints,
        sanctioned_new_paths=None,
    )
    triage_json = triage_result.model_dump_json()

    with engine.begin() as conn:
        _require_awaiting_chunk_plan_approval(conn, run_id)
        next_version = _next_plan_version(conn, run_id)
        append_plan_version(
            conn,
            run_id=run_id,
            version=next_version,
            triage_json=triage_json,
            source=PLAN_VERSION_SOURCE_PLAN_TURN,
            created_from_turn_id=recorded_turn["id"],
        )
        conn.execute(text("""
            UPDATE pipeline_runs
            SET chunk_plan = :chunk_plan,
                total_chunks = :total_chunks,
                current_chunk_number = 0
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "chunk_plan": triage_json,
            "total_chunks": triage_result.total_chunks,
        })
        replace_pending_chunks_for_plan(
            conn,
            run_id,
            project_id,
            triage_result,
        )

    return PlanTurnResult(
        run_id=run_id,
        project_id=project_id,
        version=next_version,
        turn_id=recorded_turn["id"],
        triage=triage_result,
    )
