"""
chunks.py
Routes for Phase 2B chunk planning, approval, execution, and manual resume.
"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkPlanResponse
from backend.pipeline.approval_gate import approve_gate, reject_gate
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
    reject_chunk_plan,
)
from backend.pipeline.chunked_orchestrator import (
    execute_approved_chunks,
    resume_chunked_pipeline,
)
from backend.pipeline.triage import run_triage
from backend.projects.project_store import get_project

router = APIRouter()


class ChunkedRunRequest(BaseModel):
    project_id: str
    feature_description: str


class RejectChunkPlanRequest(BaseModel):
    reason: str | None = None


class RejectFinalApprovalRequest(BaseModel):
    reason: str | None = None


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


@router.post("/runs/chunked", response_model=ChunkPlanResponse)
async def create_chunked_run_route(request: ChunkedRunRequest):
    project = get_project(request.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    run_id = str(uuid.uuid4())
    try:
        triage_result = await run_triage(
            run_id=run_id,
            project_id=request.project_id,
            feature_description=request.feature_description,
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
        return approve_chunk_plan(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/reject", response_model=ChunkPlanResponse)
def reject_chunk_plan_route(run_id: str, request: RejectChunkPlanRequest):
    try:
        return reject_chunk_plan(run_id, request.reason)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/execute")
async def execute_chunks_route(run_id: str):
    try:
        return await execute_approved_chunks(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/resume")
async def resume_chunks_route(run_id: str):
    try:
        return await resume_chunked_pipeline(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/final-approval/approve")
def approve_final_approval_route(run_id: str):
    try:
        gate = _get_pending_final_gate(run_id)
        if gate is None:
            raise HTTPException(
                status_code=404,
                detail="Pending final approval gate not found",
            )
        if not approve_gate(gate["id"]):
            raise HTTPException(
                status_code=400,
                detail="Final approval gate could not be approved",
            )
        _update_run_final_status(run_id, "final_approved")
        return {"status": "final_approved", "run_id": run_id}
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
        gate = _get_pending_final_gate(run_id)
        if gate is None:
            raise HTTPException(
                status_code=404,
                detail="Pending final approval gate not found",
            )
        if not reject_gate(gate["id"], request.reason or "Final approval rejected"):
            raise HTTPException(
                status_code=400,
                detail="Final approval gate could not be rejected",
            )
        _update_run_final_status(run_id, "final_rejected")
        return {"status": "final_rejected", "run_id": run_id}
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))
