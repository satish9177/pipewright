"""
memory.py
Project-scoped memory management routes.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from backend.memory.bootstrap import (
    ALLOWED_SUGGESTION_STATUSES,
    approve_suggestion,
    generate_bootstrap_suggestions,
    list_suggestions,
    reject_suggestion,
)
from backend.memory.memory_store import (
    ALLOWED_CATEGORIES,
    ALLOWED_SCOPES,
    ALLOWED_STATUSES,
    DEFAULT_PRIORITY,
    add_fact,
    archive_fact,
    list_facts,
    update_fact,
    verify_fact,
)
from backend.memory.prompt_builder import ROLE_CATEGORIES, build_project_memory_block
from backend.memory.repo_reality import verify_project_db_memory_against_repo
from backend.projects.project_store import get_project

router = APIRouter(prefix="/api/v1/projects/{project_id}/memory")


class MemoryFactResponse(BaseModel):
    id: str
    project_id: str
    content: str
    category: str
    scope: str
    priority: int
    status: str
    source: str | None = None
    added_by: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    last_verified_at: str | None = None
    archived_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemoryFactListResponse(BaseModel):
    project_id: str
    facts: list[MemoryFactResponse]


class MemoryFactCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=400)
    category: str = "other"
    scope: str = "global"
    priority: int = Field(default=DEFAULT_PRIORITY, ge=0, le=1000)
    source: str = "manual"

    @field_validator("category")
    @classmethod
    def category_must_be_allowed(cls, value: str) -> str:
        return _validate_allowed(value, ALLOWED_CATEGORIES, "category")

    @field_validator("scope")
    @classmethod
    def scope_must_be_allowed(cls, value: str) -> str:
        return _validate_allowed(value, ALLOWED_SCOPES, "scope")


class MemoryFactUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=400)
    category: str | None = None
    scope: str | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)

    @field_validator("category")
    @classmethod
    def category_must_be_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_allowed(value, ALLOWED_CATEGORIES, "category")

    @field_validator("scope")
    @classmethod
    def scope_must_be_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_allowed(value, ALLOWED_SCOPES, "scope")

    @model_validator(mode="after")
    def at_least_one_field(self) -> "MemoryFactUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("At least one memory field must be provided")
        return self


class MemoryArchiveRequest(BaseModel):
    reason: str = Field(min_length=4, max_length=400)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Archive reason is required")
        return value


class MemoryVerifyResponse(BaseModel):
    id: str
    project_id: str
    last_verified_at: str


class MemoryPromptPreviewResponse(BaseModel):
    project_id: str
    role: str | None
    memory_block: str
    empty: bool


class RepoRealityEvidence(BaseModel):
    fact_id: str
    memory_value: str
    repo_value: str
    evidence_path: str | None = None
    evidence_excerpt: str | None = None


class MemoryVerifyRepoResponse(BaseModel):
    project_id: str
    repo_db_signal: str | None
    ambiguous: bool
    checked_count: int
    verified_fact_ids: list[str]
    staled_fact_ids: list[str]
    skipped_fact_ids: list[str]
    warnings: list[str]
    evidence: list[RepoRealityEvidence]


class MemoryBootstrapRequest(BaseModel):
    force: bool = False


class MemorySuggestionResponse(BaseModel):
    id: str
    project_id: str
    content: str
    category: str
    scope: str
    priority: int
    source: str | None = None
    evidence_path: str | None = None
    evidence_excerpt: str | None = None
    status: str
    created_at: str | None = None
    updated_at: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    rejected_by: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    edited_content: str | None = None
    approved_fact_id: str | None = None


class MemorySuggestionListResponse(BaseModel):
    project_id: str
    suggestions: list[MemorySuggestionResponse]


class MemorySuggestionApprovalResponse(BaseModel):
    suggestion: MemorySuggestionResponse
    fact: MemoryFactResponse


class MemorySuggestionApproveRequest(BaseModel):
    edited_content: str | None = Field(default=None, max_length=400)
    approved_by: str | None = None


class MemorySuggestionRejectRequest(BaseModel):
    reason: str = Field(min_length=4, max_length=400)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Rejection reason is required")
        return value


def _validate_allowed(value: str, allowed_values: set[str], field_name: str) -> str:
    normalized = (value or "").strip()
    if normalized not in allowed_values:
        raise ValueError(f"Invalid memory {field_name}")
    return normalized


def _require_project(project_id: str) -> dict:
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _sanitize_fact(fact: dict) -> dict:
    return {
        key: value
        for key, value in fact.items()
        if key != "content_hash"
    }


def _get_fact_for_project(project_id: str, memory_id: str) -> dict:
    for fact in list_facts(project_id):
        if fact["id"] == memory_id:
            return fact
    raise HTTPException(status_code=404, detail="Memory fact not found")


def _map_memory_error(error: ValueError) -> HTTPException:
    message = str(error)
    if "active duplicate memory fact already exists" in message:
        return HTTPException(
            status_code=409,
            detail="Active duplicate memory fact already exists",
        )
    if "memory fact not found" in message:
        return HTTPException(status_code=404, detail="Memory fact not found")
    if "suggestion not found" in message:
        return HTTPException(status_code=404, detail="Memory suggestion not found")
    if "suggestion is not pending" in message:
        return HTTPException(status_code=409, detail="Memory suggestion is not pending")
    return HTTPException(status_code=422, detail=message)


@router.get("", response_model=MemoryFactListResponse)
def list_memory_facts(
    project_id: str,
    status: str | None = Query(default=None),
    category: str | None = Query(default=None),
    scope: str | None = Query(default=None),
):
    _require_project(project_id)
    if status is not None:
        _validate_allowed(status, ALLOWED_STATUSES, "status")
    if category is not None:
        category = _validate_allowed(category, ALLOWED_CATEGORIES, "category")
    if scope is not None:
        scope = _validate_allowed(scope, ALLOWED_SCOPES, "scope")

    try:
        facts = list_facts(
            project_id,
            status=status,
            category=category,
            scope=scope,
        )
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))

    return {
        "project_id": project_id,
        "facts": [_sanitize_fact(fact) for fact in facts],
    }


@router.post("", response_model=MemoryFactResponse)
def create_memory_fact(project_id: str, request: MemoryFactCreateRequest):
    _require_project(project_id)
    try:
        fact = add_fact(
            project_id=project_id,
            content=request.content,
            category=request.category,
            scope=request.scope,
            priority=request.priority,
            source=request.source,
            added_by="api",
            approved_by="api",
        )
        return _sanitize_fact(_get_fact_for_project(project_id, fact["id"]))
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post(
    "/bootstrap-suggestions",
    response_model=MemorySuggestionListResponse,
)
def create_bootstrap_suggestions(
    project_id: str,
    request: MemoryBootstrapRequest,
):
    _require_project(project_id)
    try:
        suggestions = generate_bootstrap_suggestions(
            project_id,
            force=request.force,
        )
        return {"project_id": project_id, "suggestions": suggestions}
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/verify-repo", response_model=MemoryVerifyRepoResponse)
def verify_memory_against_repo(project_id: str):
    """
    Manual, explicit verification of active DB memory against the current repo
    DB fingerprint. Uses the project's stored repo_path. Never blocks a run,
    never edits or archives memory, never changes prompt format. Conflicting DB
    facts are marked stale (excluded from prompts by the existing builder).
    """
    project = _require_project(project_id)
    try:
        return verify_project_db_memory_against_repo(
            project_id=project_id,
            repo_path=project.get("repo_path"),
        )
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.get("/suggestions", response_model=MemorySuggestionListResponse)
def list_memory_suggestions(
    project_id: str,
    status: str | None = Query(default=None),
):
    _require_project(project_id)
    if status is not None:
        _validate_allowed(status, ALLOWED_SUGGESTION_STATUSES, "suggestion status")
    try:
        suggestions = list_suggestions(project_id, status=status)
        return {"project_id": project_id, "suggestions": suggestions}
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.get("/prompt-preview", response_model=MemoryPromptPreviewResponse)
def preview_memory_prompt(
    project_id: str,
    role: str | None = Query(default=None),
    token_budget: int | None = Query(default=None, ge=1),
):
    project = _require_project(project_id)
    if role is not None and role not in ROLE_CATEGORIES:
        raise HTTPException(status_code=422, detail="Invalid memory role")

    memory_block = build_project_memory_block(
        project_id=project_id,
        role=role,
        project_name=project.get("name"),
        token_budget=token_budget,
    )
    return {
        "project_id": project_id,
        "role": role,
        "memory_block": memory_block,
        "empty": memory_block == "",
    }


@router.post(
    "/suggestions/{suggestion_id}/approve",
    response_model=MemorySuggestionApprovalResponse,
)
def approve_memory_suggestion(
    project_id: str,
    suggestion_id: str,
    request: MemorySuggestionApproveRequest | None = Body(default=None),
):
    _require_project(project_id)
    try:
        return approve_suggestion(
            project_id=project_id,
            suggestion_id=suggestion_id,
            approved_by=(request.approved_by if request and request.approved_by else "api"),
            edited_content=(request.edited_content if request else None),
        )
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post(
    "/suggestions/{suggestion_id}/reject",
    response_model=MemorySuggestionResponse,
)
def reject_memory_suggestion(
    project_id: str,
    suggestion_id: str,
    request: MemorySuggestionRejectRequest,
):
    _require_project(project_id)
    try:
        return reject_suggestion(
            project_id=project_id,
            suggestion_id=suggestion_id,
            reason=request.reason,
            rejected_by="api",
        )
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.patch("/{memory_id}", response_model=MemoryFactResponse)
def update_memory_fact(
    project_id: str,
    memory_id: str,
    request: MemoryFactUpdateRequest,
):
    _require_project(project_id)
    values: dict[str, Any] = request.model_dump(exclude_unset=True)
    try:
        fact = update_fact(project_id=project_id, memory_id=memory_id, **values)
        return _sanitize_fact(fact)
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/{memory_id}/archive", response_model=MemoryFactResponse)
def archive_memory_fact(
    project_id: str,
    memory_id: str,
    request: MemoryArchiveRequest,
):
    _require_project(project_id)
    try:
        archive_fact(
            project_id=project_id,
            memory_id=memory_id,
            reason=request.reason,
            archived_by="api",
        )
        return _sanitize_fact(_get_fact_for_project(project_id, memory_id))
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/{memory_id}/verify", response_model=MemoryVerifyResponse)
def verify_memory_fact(project_id: str, memory_id: str):
    _require_project(project_id)
    try:
        return verify_fact(
            project_id=project_id,
            memory_id=memory_id,
            verified_by="api",
        )
    except ValueError as error:
        raise _map_memory_error(error)
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))
