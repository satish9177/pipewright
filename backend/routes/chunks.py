"""
chunks.py
Routes for Phase 2B chunk planning, approval, execution, and manual resume.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from backend.core.statuses import ApprovalStatus, RunStatus
from backend.db.database import engine
from backend.models.handoff import (
    FEATURE_DESCRIPTION_MAX_LENGTH,
    REJECTION_REASON_MAX_LENGTH,
    _is_blank,
)
from backend.models.chunk import ChunkPlanResponse
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
    reject_chunk_plan,
)
from backend.pipeline.chunked_orchestrator import (
    approve_chunk_and_commit,
    execute_approved_chunks,
    reject_chunk_and_rollback,
    resume_chunked_pipeline,
)
from backend.pipeline.pr_orchestrator import push_and_create_pr
from backend.pipeline.intent import (
    IMPLEMENTATION,
    PLAN_ONLY,
    REPORT_ONLY,
    classify_intent,
)
from backend.pipeline.risk_scanner import scan_triage_result
from backend.pipeline.run_locks import ProjectRepoLockError
from backend.pipeline.triage import run_triage
from backend.projects.project_store import get_project

router = APIRouter()
READ_ONLY_EXECUTION_MESSAGE = "This run is read-only and cannot execute code changes."


class ChunkedRunRequest(BaseModel):
    project_id: str
    feature_description: str = Field(
        min_length=1,
        max_length=FEATURE_DESCRIPTION_MAX_LENGTH,
    )

    @field_validator("feature_description")
    @classmethod
    def feature_description_must_not_be_blank(cls, value: str) -> str:
        if _is_blank(value):
            raise ValueError("Field must not be blank")
        return value


class RejectChunkPlanRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectFinalApprovalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectChunkApprovalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


def _get_pending_final_gate(run_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND chunk_number = 0
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """), {"run_id": run_id}).fetchone()
    return dict(row._mapping) if row else None


def _update_run_final_status(run_id: str, status: str) -> None:
    with engine.connect() as conn:
        result = conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = :current_step
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "status": status,
            "current_step": status,
        })
        conn.commit()
    if result.rowcount == 0:
        raise ValueError(f"Run not found: {run_id}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_read_only_report(project: dict, feature_description: str) -> str:
    lines = [
        "Read-only report request recorded.",
        "",
        f"Project: {project.get('name') or project.get('id')}",
        f"Repository path: {project.get('repo_path')}",
        "",
        "Request:",
        feature_description,
        "",
        "No code changes, tests, Git operations, push, or PR creation were run.",
    ]
    return "\n".join(lines)


def _create_read_only_run(
    run_id: str,
    project_id: str,
    feature_description: str,
    intent: str,
    status: str,
    summary: str | None = None,
    chunk_plan: str | None = None,
    total_chunks: int = 0,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (
                id, project_id, feature_description, plain_english_summary,
                status, current_step, intent, chunk_plan_status, chunk_plan,
                total_chunks, current_chunk_number, created_at
            )
            VALUES
            (
                :id, :project_id, :feature_description, :summary,
                :status, :current_step, :intent, 'none', :chunk_plan,
                :total_chunks, 0, :created_at
            )
        """), {
            "id": run_id,
            "project_id": project_id,
            "feature_description": feature_description,
            "summary": summary,
            "status": status,
            "current_step": status,
            "intent": intent,
            "chunk_plan": chunk_plan,
            "total_chunks": total_chunks,
            "created_at": _utc_now(),
        })


def _load_run_intent(run_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT intent FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    if row is None:
        return IMPLEMENTATION
    return row[0] or IMPLEMENTATION


def _ensure_mutating_run(run_id: str) -> None:
    if _load_run_intent(run_id) in {REPORT_ONLY, PLAN_ONLY}:
        raise RuntimeError(READ_ONLY_EXECUTION_MESSAGE)


def _decide_final_gate(
    run_id: str,
    gate_status: str,
    run_status: str,
    reason: str | None = None,
) -> dict:
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT * FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND chunk_number = 0
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """), {"run_id": run_id}).fetchone()
        if row is None:
            raise ValueError("Pending final approval gate not found")

        gate = dict(row._mapping)
        conn.execute(text("""
            UPDATE approval_gates
            SET status = :gate_status,
                rejection_reason = :reason,
                decided_at = :decided_at
            WHERE id = :gate_id
              AND status = 'pending'
        """), {
            "gate_status": gate_status,
            "reason": reason,
            "decided_at": _utc_now(),
            "gate_id": gate["id"],
        })
        result = conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :run_status,
                current_step = :run_status
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "run_status": run_status,
        })
        if result.rowcount == 0:
            raise ValueError(f"Run not found: {run_id}")
        return {"status": run_status, "run_id": run_id}


@router.post("/runs/chunked", response_model=ChunkPlanResponse)
async def create_chunked_run_route(request: ChunkedRunRequest):
    project = get_project(request.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    run_id = str(uuid.uuid4())
    intent = classify_intent(request.feature_description)
    try:
        if intent == REPORT_ONLY:
            report = _build_read_only_report(project, request.feature_description)
            _create_read_only_run(
                run_id=run_id,
                project_id=request.project_id,
                feature_description=request.feature_description,
                intent=REPORT_ONLY,
                status=RunStatus.REPORT_READY,
                summary=report,
            )
            return ChunkPlanResponse(
                run_id=run_id,
                project_id=request.project_id,
                chunk_plan_status="none",
                total_chunks=0,
                current_chunk_number=0,
                triage=None,
                chunks=[],
            )

        triage_result = await run_triage(
            run_id=run_id,
            project_id=request.project_id,
            feature_description=request.feature_description,
        )
        triage_result = scan_triage_result(triage_result)
        if intent == PLAN_ONLY:
            _create_read_only_run(
                run_id=run_id,
                project_id=request.project_id,
                feature_description=request.feature_description,
                intent=PLAN_ONLY,
                status=RunStatus.PLAN_READY,
                summary=triage_result.reasoning,
                chunk_plan=triage_result.model_dump_json(),
                total_chunks=triage_result.total_chunks,
            )
            return ChunkPlanResponse(
                run_id=run_id,
                project_id=request.project_id,
                chunk_plan_status="none",
                total_chunks=triage_result.total_chunks,
                current_chunk_number=0,
                triage=triage_result,
                chunks=[],
            )

        return create_chunked_run(
            run_id=run_id,
            project_id=request.project_id,
            feature_description=request.feature_description,
            triage_result=triage_result,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.get("/runs/{run_id}/chunks", response_model=ChunkPlanResponse)
def get_chunk_plan_route(run_id: str):
    try:
        return get_chunk_plan_status(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/runs/{run_id}/chunks/approve", response_model=ChunkPlanResponse)
def approve_chunk_plan_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return approve_chunk_plan(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/reject", response_model=ChunkPlanResponse)
def reject_chunk_plan_route(run_id: str, request: RejectChunkPlanRequest):
    try:
        _ensure_mutating_run(run_id)
        return reject_chunk_plan(run_id, request.reason)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/execute")
async def execute_chunks_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return await execute_approved_chunks(run_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/resume")
async def resume_chunks_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return await resume_chunked_pipeline(run_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/{chunk_number}/approve")
def approve_chunk_route(run_id: str, chunk_number: int):
    try:
        _ensure_mutating_run(run_id)
        return approve_chunk_and_commit(run_id, chunk_number)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/{chunk_number}/reject")
def reject_chunk_route(
    run_id: str,
    chunk_number: int,
    request: RejectChunkApprovalRequest,
):
    try:
        _ensure_mutating_run(run_id)
        return reject_chunk_and_rollback(run_id, chunk_number, request.reason)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/final-approval/approve")
def approve_final_approval_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return _decide_final_gate(
            run_id,
            ApprovalStatus.APPROVED,
            RunStatus.FINAL_APPROVED,
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/final-approval/reject")
def reject_final_approval_route(
    run_id: str,
    request: RejectFinalApprovalRequest,
):
    try:
        _ensure_mutating_run(run_id)
        return _decide_final_gate(
            run_id,
            ApprovalStatus.REJECTED,
            RunStatus.FINAL_REJECTED,
            request.reason or "Final approval rejected",
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/push-pr")
def push_pr_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return push_and_create_pr(run_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))
