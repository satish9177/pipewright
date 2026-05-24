"""
schema.py
Pydantic event contract for live run observability.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


EventKind = Literal[
    "stage_started",
    "stage_completed",
    "stage_failed",
    "log",
    "heartbeat",
    "run_status_changed",
    "chunk_status_changed",
    "chunk_plan_created",
    "chunk_plan_approved",
    "chunk_plan_rejected",
    "approval_required",
    "approval_decided",
    "git_operation",
    "github_operation",
    "rollback",
    "terminal",
]

EventStage = Literal[
    "triage",
    "planner",
    "coder",
    "patch",
    "test",
    "approval",
    "git",
    "github",
    "orchestrator",
    "system",
]


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, default=str, sort_keys=True))


class Event(BaseModel):
    id: str = Field(default_factory=_new_event_id)
    ts: str = Field(default_factory=_utc_iso)
    run_id: str
    chunk_number: int | None = None
    kind: EventKind
    stage: EventStage | None = None
    level: str = "info"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _truncate_message(cls, value: str) -> str:
        if value is None:
            return ""
        text = str(value)
        return text[:500]

    @field_validator("data")
    @classmethod
    def _cap_data_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            return {}
        try:
            if _json_size(value) <= 4000:
                return value
            preview = json.dumps(value, default=str, sort_keys=True)[:1000]
            return {
                "truncated": True,
                "preview": preview,
            }
        except Exception:
            return {
                "truncated": True,
                "preview": str(value)[:1000],
            }
