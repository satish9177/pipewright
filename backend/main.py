"""
main.py
FastAPI application entry point.
Initializes database on startup.
"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from backend.core.config import get_config
from backend.core.logging_config import configure_logging
from backend.db.database import init_db
from backend.db.database import engine
from backend.runtime.approval_gate_recovery import timeout_stale_approval_gates
from backend.runtime.startup_recovery import recover_interrupted_runs
from backend.models.handoff import RejectRequest
from backend.pipeline.approval_gate import (
    approve_gate,
    reject_gate,
    get_pending_gates,
    get_gate,
)
from backend.routes.chunks import router as chunks_router
from backend.routes.memory import router as memory_router
from backend.routes.projects import router as projects_router
from backend.routes.ws_events import router as ws_events_router


configure_logging()
logger = logging.getLogger(__name__)
app_config = get_config()


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
app.include_router(memory_router)
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
async def start_pipeline_disabled():
    """
    Legacy single-shot pipeline endpoint — permanently disabled (HTTP 410).

    The legacy /run path bypassed the Phase 2 chunked safety flow: chunk-plan
    approval, the files_expected scope guard, deterministic high-risk gating,
    the ambiguous-implementation guard, and final approval — and it auto-created
    a PR after a single approval. It is disabled to preserve those guards. The
    only supported implementation path is POST /runs/chunked.

    This handler takes no request body and starts no pipeline work; it returns
    410 immediately so no patch / test / commit / PR path is ever reached.
    """
    raise HTTPException(
        status_code=410,
        detail="Legacy run endpoint is disabled. Use /runs/chunked.",
    )


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
