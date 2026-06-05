"""
statuses.py
Centralized string status constants.

These are plain strings by design. Database rows and API responses already use
string values, so this module must not introduce Enum serialization behavior.
"""


class RunStatus:
    RUNNING = "running"
    RUNNING_CHUNKS = "running_chunks"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"
    FAILED = "failed"
    REJECTED = "rejected"
    COMPLETE = "complete"
    STARTED = "started"
    REPORT_READY = "report_ready"
    PLAN_READY = "plan_ready"
    AWAITING_CHUNK_PLAN_APPROVAL = "awaiting_chunk_plan_approval"
    CHUNK_PLAN_APPROVED = "chunk_plan_approved"
    AWAITING_CHUNK_APPROVAL = "awaiting_chunk_approval"
    # Run-level surfacing for an eligible clean SCOPE_VIOLATION that produced a
    # pending scope_expansion_request (#27). The chunk itself stays `failed`
    # (design section 10); this run status only lets the UI/API distinguish "scope
    # expansion pending" from an ordinary patch failure. It does not unblock
    # dependents and grants no execution authority.
    AWAITING_SCOPE_APPROVAL = "awaiting_scope_approval"
    CHUNK_APPROVED = "chunk_approved"
    AWAITING_FINAL_APPROVAL = "awaiting_final_approval"
    AWAITING_MEMORY_CONFLICT_APPROVAL = "awaiting_memory_conflict_approval"
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
