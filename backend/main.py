"""
main.py
FastAPI application entry point.
Initializes database on startup.
"""

from fastapi import FastAPI, HTTPException
from backend.db.database import init_db
from backend.models.handoff import RejectRequest
from backend.pipeline.approval_gate import (
    approve_gate,
    reject_gate,
    get_pending_gates,
    get_gate,
)

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
