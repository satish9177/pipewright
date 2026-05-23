"""
chunks.py
Routes for Phase 2B chunk plan creation and approval.

These endpoints stop at chunk plan approval. They do not execute chunks.
"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.models.chunk import ChunkPlanResponse
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
    reject_chunk_plan,
)
from backend.pipeline.chunked_orchestrator import execute_approved_chunks
from backend.pipeline.triage import run_triage
from backend.projects.project_store import get_project

router = APIRouter()


class ChunkedRunRequest(BaseModel):
    project_id: str
    feature_description: str


class RejectChunkPlanRequest(BaseModel):
    reason: str | None = None


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
