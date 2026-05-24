"""
main.py
FastAPI application entry point.
Initializes database on startup.
"""

import uuid
from contextlib import asynccontextmanager
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from backend.db.database import init_db
from backend.db.database import engine
from backend.models.handoff import ProjectCreate, ProjectUpdate, RejectRequest
from backend.pipeline.approval_gate import (
    approve_gate,
    reject_gate,
    get_pending_gates,
    get_gate,
)
from backend.pipeline.orchestrator import _run_pipeline_with_id
from backend.projects.project_store import (
    create_project,
    get_project,
    list_projects as list_stored_projects,
    update_project,
)
from backend.routes.chunks import router as chunks_router
from backend.routes.ws_events import router as ws_events_router


class RunRequest(BaseModel):
    project_id: str
    feature_description: str

@asynccontextmanager
async def lifespan(app):
    init_db()
    # Clean up stale gates from crashed sessions
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE approval_gates
            SET status = 'timeout'
            WHERE status = 'pending'
            AND created_at < datetime('now', '-2 hours')
        """))
        conn.commit()
    print("Pipewright started.")
    yield


app = FastAPI(
    title="Pipewright",
    description="AI pipeline that orchestrates multiple models with human approval.",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chunks_router)
app.include_router(ws_events_router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/projects")
def create_project_route(request: ProjectCreate):
    try:
        return create_project(
            name=request.name,
            repo_path=request.repo_path,
            test_command=request.test_command,
            branch=request.branch,
            description=request.description,
            github_token=request.github_token,
            github_owner=request.github_owner,
            github_repo=request.github_repo,
            github_base_branch=request.github_base_branch,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/projects")
def list_projects_route():
    return list_stored_projects()


@app.get("/projects/{project_id}")
def get_project_route(project_id: str):
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.patch("/projects/{project_id}")
def update_project_endpoint(
    project_id: str,
    request: ProjectUpdate
):
    """
    Update project fields.
    Use this to add GitHub credentials to a project.
    Only fields provided are updated.
    Fields not provided keep their existing values.
    """
    from fastapi import HTTPException
    project = update_project(
        project_id=project_id,
        name=request.name,
        test_command=request.test_command,
        branch=request.branch,
        description=request.description,
        github_token=request.github_token,
        github_owner=request.github_owner,
        github_repo=request.github_repo,
        github_base_branch=request.github_base_branch
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail="Project not found"
        )
    return project


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

    async def pipeline_task():
        await _run_pipeline_with_id(
            request.feature_description,
            run_id,
            request.project_id
        )

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
