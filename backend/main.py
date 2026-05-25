"""
main.py
FastAPI application entry point.
Initializes database on startup.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field, field_validator
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from backend.core.config import get_config
from backend.core.logging_config import configure_logging
from backend.db.database import init_db
from backend.db.database import engine
from backend.runtime.approval_gate_recovery import timeout_stale_approval_gates
from backend.runtime.startup_recovery import recover_interrupted_runs
from backend.models.handoff import (
    RejectRequest,
    FEATURE_DESCRIPTION_MAX_LENGTH,
    _is_blank,
)
from backend.pipeline.approval_gate import (
    approve_gate,
    reject_gate,
    get_pending_gates,
    get_gate,
)
from backend.pipeline.orchestrator import _run_pipeline_with_id
from backend.pipeline.run_locks import ProjectRepoLockError, project_repo_lock_sync
from backend.projects.project_store import (
    get_project,
)
from backend.routes.chunks import router as chunks_router
from backend.routes.projects import router as projects_router
from backend.routes.ws_events import router as ws_events_router


configure_logging()
logger = logging.getLogger(__name__)
app_config = get_config()


class RunRequest(BaseModel):
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

@asynccontextmanager
async def lifespan(app):
    init_db()
    timeout_stale_approval_gates()
    recover_interrupted_runs()
    logger.info("Pipewright started.")
    yield


app = FastAPI(
    title=app_config.app_name,
    description="AI pipeline that orchestrates multiple models with human approval.",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(app_config.cors_allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chunks_router)
app.include_router(projects_router)
app.include_router(ws_events_router)


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).scalar_one()
        return {"status": "ok", "version": "0.1.0", "database": "ok"}
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable: {error}",
        )


@app.post("/run")
async def start_pipeline(request: RunRequest, background_tasks: BackgroundTasks):
    """
    Start a new pipeline run.
    Runs in background so HTTP response
    returns immediately with run_id.
    Pipeline continues in background.
    """
    project = get_project(request.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    run_id = str(uuid.uuid4())

    try:
        reservation = project_repo_lock_sync(request.project_id)
        reservation.__enter__()
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))

    async def pipeline_task():
        try:
            await _run_pipeline_with_id(
                request.feature_description,
                run_id,
                request.project_id,
                lock_already_held=True,
            )
        finally:
            reservation.__exit__(None, None, None)

    background_tasks.add_task(pipeline_task)

    return {
        "run_id": run_id,
        "project_id": request.project_id,
        "status": "started",
        "message": "Pipeline started. Watch terminal for progress."
    }


@app.get("/runs/{run_id}")
def get_run_status(run_id: str):
    """
    Get current status of a pipeline run.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT * FROM pipeline_runs WHERE id = :id"
        ), {"id": run_id}).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    return dict(row._mapping)


@app.get("/runs")
def list_runs():
    """
    List all pipeline runs ordered by most recent.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM pipeline_runs ORDER BY created_at DESC LIMIT 20"
        )).fetchall()

    return [dict(row._mapping) for row in rows]


@app.get("/gates")
def list_gates():
    return get_pending_gates()


@app.get("/gates/{gate_id}")
def read_gate(gate_id: str):
    gate = get_gate(gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="Gate not found")
    return gate


@app.post("/gates/{gate_id}/approve")
def approve(gate_id: str):
    updated = approve_gate(gate_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Gate not found")
    return {"status": "approved", "gate_id": gate_id}


@app.post("/gates/{gate_id}/reject")
def reject(gate_id: str, request: RejectRequest):
    updated = reject_gate(gate_id, request.reason)
    if not updated:
        raise HTTPException(status_code=404, detail="Gate not found")
    return {"status": "rejected", "gate_id": gate_id, "reason": request.reason}
