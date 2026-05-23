"""
main.py
FastAPI application entry point.
Initializes database on startup.
"""

import uuid
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks
from sqlalchemy import text
from backend.db.database import init_db
from backend.db.database import engine
from backend.models.handoff import RejectRequest
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
)


class RunRequest(BaseModel):
    project_id: str
    feature_description: str


class ProjectRequest(BaseModel):
    name: str
    repo_path: str
    test_command: str


app = FastAPI(
    title="Pipewright",
    description="AI pipeline that orchestrates multiple models with human approval.",
    version="0.1.0"
)


@app.on_event("startup")
def startup():
    init_db()
    print("Pipewright started.")


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/projects")
def create_project_route(request: ProjectRequest):
    try:
        return create_project(
            name=request.name,
            repo_path=request.repo_path,
            test_command=request.test_command,
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
