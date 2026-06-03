"""
chunk.py
Pydantic contracts for Phase 2B chunk planning.

These models describe a proposed chunk plan only. They do not execute chunks,
apply patches, run tests, or create approvals.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ChunkDefinition(BaseModel):
    chunk_number: int = Field(..., ge=1)
    title: str
    description: str
    files_expected: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "medium"
    token_estimate: int = Field(..., ge=0)
    requires_human_review: bool = False
    rationale: str = ""

    @field_validator("title", "description")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("chunk.py: field must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _validate_chunk_rules(self):
        for dependency in self.depends_on:
            if dependency >= self.chunk_number:
                raise ValueError(
                    "chunk.py: dependencies must refer to earlier chunks only"
                )
            if dependency < 1:
                raise ValueError(
                    "chunk.py: dependencies must be positive chunk numbers"
                )

        if self.risk_level == "high" and not self.requires_human_review:
            raise ValueError(
                "chunk.py: high risk chunks require human review"
            )

        return self


class TriageResult(BaseModel):
    run_id: str
    project_id: str
    feature_description: str
    complexity: Literal["easy", "medium", "hard"]
    total_chunks: int = Field(..., ge=1)
    chunks: list[ChunkDefinition]
    reasoning: str

    @model_validator(mode="after")
    def _validate_plan_rules(self):
        if self.total_chunks != len(self.chunks):
            raise ValueError(
                "chunk.py: total_chunks must match number of chunks"
            )

        chunk_numbers = [chunk.chunk_number for chunk in self.chunks]
        expected_numbers = list(range(1, self.total_chunks + 1))
        if chunk_numbers != expected_numbers:
            raise ValueError(
                "chunk.py: chunk_numbers must be exactly 1..N with no gaps"
            )

        existing = set()
        for chunk in self.chunks:
            for dependency in chunk.depends_on:
                if dependency not in existing:
                    raise ValueError(
                        "chunk.py: dependencies must refer to existing "
                        "earlier chunks only"
                    )
            existing.add(chunk.chunk_number)

        return self


class PendingScopeExpansion(BaseModel):
    """
    Read-only view of a *pending* scope expansion request, surfaced on a failed
    chunk so the frontend can render the approve/reject UI (#27F).

    This carries only what the UI needs to act and explain: the request id and
    the chunk's failure_report_id (to call the approve/reject routes), the
    untrusted requested_files (diagnostic display only — "the previous attempt
    tried to touch these"), the lifecycle status, and the created timestamp. It
    never carries file contents, diffs, secrets, or token-like values, and it
    grants no authority on its own — approval still goes through the existing
    backend route, which re-validates everything.
    """

    request_id: str
    chunk_number: int
    failure_report_id: str
    requested_files: list[str] = Field(default_factory=list)
    status: str
    created_at: str | None = None


class ChunkStatus(BaseModel):
    run_id: str
    project_id: str
    chunk_number: int
    title: str
    status: str
    risk_level: str
    requires_human_review: bool
    files_expected: list[str]
    depends_on: list[int]
    completion_summary: str | None = None
    error_message: str | None = None
    # Read-only overlay (#27F): the single pending scope expansion request for
    # this chunk, when one exists. None for the common case. Populated by
    # chunk_store.get_chunk_plan_status; never affects effective scope or
    # execution — it only lets the UI surface the existing approve/reject flow.
    pending_scope_expansion: PendingScopeExpansion | None = None


class ChunkPlanResponse(BaseModel):
    run_id: str
    project_id: str
    chunk_plan_status: str
    total_chunks: int
    current_chunk_number: int
    triage: TriageResult | None = None
    chunks: list[ChunkStatus]
