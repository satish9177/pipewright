"""
statuses.py
Centralized string status constants.

These are plain strings by design. Database rows and API responses already use
string values, so this module must not introduce Enum serialization behavior.
"""


class RunStatus:
    RUNNING = "running"
    RUNNING_CHUNKS = "running_chunks"
    PAUSED = "paused"
    FAILED = "failed"
    REJECTED = "rejected"
    COMPLETE = "complete"
    STARTED = "started"
    AWAITING_CHUNK_PLAN_APPROVAL = "awaiting_chunk_plan_approval"
    CHUNK_PLAN_APPROVED = "chunk_plan_approved"
    AWAITING_CHUNK_APPROVAL = "awaiting_chunk_approval"
    CHUNK_APPROVED = "chunk_approved"
    AWAITING_FINAL_APPROVAL = "awaiting_final_approval"
    FINAL_APPROVED = "final_approved"
    FINAL_REJECTED = "final_rejected"
    PUSHING = "pushing"
    PUSH_FAILED = "push_failed"


class ChunkStatusValue:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    AWAITING_CHUNK_APPROVAL = "awaiting_chunk_approval"


class ChunkPlanStatus:
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    NONE = "none"


class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class GateStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class ProjectStatus:
    ACTIVE = "active"


class CheckpointStatus:
    COMPLETE = "complete"
