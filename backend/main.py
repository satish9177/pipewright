"""
main.py
FastAPI application entry point.
Initializes database on startup.
"""

from fastapi import FastAPI
from backend.db.database import init_db

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
