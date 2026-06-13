"""
handoff.py
Pydantic models for all inter-module handoff contracts.
Every model call produces one of these.
Every downstream module receives one of these.
Never use raw dicts between pipeline modules.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, List, Optional

from backend.projects.pr_modes import (
    DEFAULT_PR_MODE,
    normalize_pr_mode,
)


PROJECT_NAME_MAX_LENGTH = 120
PROJECT_REPO_PATH_MAX_LENGTH = 1000
PROJECT_TEST_COMMAND_MAX_LENGTH = 1000
PROJECT_DESCRIPTION_MAX_LENGTH = 2000
GITHUB_OWNER_MAX_LENGTH = 100
GITHUB_REPO_MAX_LENGTH = 100
GITHUB_BASE_BRANCH_MAX_LENGTH = 100
GITHUB_TOKEN_MAX_LENGTH = 5000
FEATURE_DESCRIPTION_MAX_LENGTH = 12000
REJECTION_REASON_MAX_LENGTH = 2000


def _is_blank(value: str) -> bool:
    return not value.strip()


def normalize_suggested_memory_category(value: Any) -> str:
    from backend.memory.memory_store import ALLOWED_CATEGORIES

    if not isinstance(value, str):
        return "other"
    normalized = value.strip().lower()
    return normalized if normalized in ALLOWED_CATEGORIES else "other"


def normalize_suggested_memory_scope(value: Any) -> str:
    from backend.memory.memory_store import ALLOWED_SCOPES

    if not isinstance(value, str):
        return "global"
    normalized = value.strip().lower()
    return normalized if normalized in ALLOWED_SCOPES else "global"


class PlannerHandoff(BaseModel):
    handoff_from: str = "planner"
    handoff_to: str = "coder"
    run_id: str
    feature_description: str
    goal: str
    steps: List[str]
    files_to_create: List[str] = Field(default_factory=list)
    files_to_modify: List[str] = Field(default_factory=list)
    files_to_read: List[str] = Field(default_factory=list)
    out_of_scope: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    suggested_memory_entries: List[str] = Field(default_factory=list)


class FileChange(BaseModel):
    path: str
    action: str  # create / modify / delete / edit
    content: Optional[str] = None
    # Targeted-edit fields. Used only when action == "edit".
    # An edit replaces exactly one occurrence of old_string with new_string,
    # so the model never has to return full file content for an existing file.
    old_string: Optional[str] = None
    new_string: Optional[str] = None
    reason: str

    @model_validator(mode="after")
    def edit_requires_old_and_new_string(self) -> "FileChange":
        if self.action == "edit":
            if self.old_string is None or self.new_string is None:
                raise ValueError(
                    "edit action requires both old_string and new_string"
                )
        return self


class SuggestedMemoryEntry(BaseModel):
    content: str
    category: str = "other"
    scope: str = "global"
    rationale: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def legacy_string_to_object(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"content": value}
        return value

    @field_validator("content", mode="before")
    @classmethod
    def content_must_be_string(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("content must be a string")
        normalized = value.strip()
        if _is_blank(normalized):
            raise ValueError("content must not be blank")
        return normalized

    @field_validator("category", mode="before")
    @classmethod
    def category_must_be_allowed(cls, value: Any) -> str:
        return normalize_suggested_memory_category(value)

    @field_validator("scope", mode="before")
    @classmethod
    def scope_must_be_allowed(cls, value: Any) -> str:
        return normalize_suggested_memory_scope(value)

    @field_validator("rationale", mode="before")
    @classmethod
    def rationale_is_optional_metadata(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None


class CoderHandoff(BaseModel):
    handoff_from: str = "coder"
    handoff_to: str = "patch_applier"
    run_id: str
    feature_description: str
    files_changed: List[FileChange]
    summary: str
    suggested_memory_entries: List[SuggestedMemoryEntry] = Field(
        default_factory=list
    )


class PatchResult(BaseModel):
    handoff_from: str = "patch_applier"
    handoff_to: str = "tester"
    run_id: str
    success: bool
    diff: str
    pre_patch_git_hash: str
    post_patch_git_hash: str
    files_applied: List[str]
    rollback_available: bool = True


class PipelineTestResult(BaseModel):
    handoff_from: str = "tester"
    handoff_to: str = "approval"
    run_id: str
    passed: bool
    exit_code: Optional[int] = None
    timed_out: bool = False
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    failing_test_ids: List[str] = Field(default_factory=list)
    failing_test_ids_parseable: bool = True
    failing_test_ids_parse_error: Optional[str] = None
    baseline_accepted: bool = False
    baseline_failing_test_ids: List[str] = Field(default_factory=list)
    pre_existing_failing_test_ids: List[str] = Field(default_factory=list)
    newly_failing_test_ids: List[str] = Field(default_factory=list)
    output: str
    duration_seconds: float = 0.0


class ApprovalRequest(BaseModel):
    gate_id: Optional[str] = None
    run_id: str
    diff: str
    test_results: PipelineTestResult
    ai_summary: str
    plain_english_summary: str
    risk_level: str = "medium"
    approved: Optional[bool] = None
    rejection_reason: Optional[str] = None


class RejectRequest(BaseModel):
    reason: str = Field(max_length=REJECTION_REASON_MAX_LENGTH)


class GateStatus(BaseModel):
    gate_id: str
    run_id: str
    status: str
    created_at: str
    diff: Optional[str] = None
    test_results: Optional[str] = None
    ai_summary: Optional[str] = None
    risk_level: str = "medium"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)
    repo_path: str = Field(min_length=1, max_length=PROJECT_REPO_PATH_MAX_LENGTH)
    test_command: str = Field(min_length=1, max_length=PROJECT_TEST_COMMAND_MAX_LENGTH)
    branch: str = Field(default="main", min_length=1, max_length=GITHUB_BASE_BRANCH_MAX_LENGTH)
    description: str = Field(default="", max_length=PROJECT_DESCRIPTION_MAX_LENGTH)
    github_token: Optional[str] = Field(default=None, max_length=GITHUB_TOKEN_MAX_LENGTH)
    github_owner: Optional[str] = Field(default=None, max_length=GITHUB_OWNER_MAX_LENGTH)
    github_repo: Optional[str] = Field(default=None, max_length=GITHUB_REPO_MAX_LENGTH)
    github_base_branch: str = Field(
        default="pipewright-staging",
        min_length=1,
        max_length=GITHUB_BASE_BRANCH_MAX_LENGTH,
    )
    pr_mode: str = Field(default=DEFAULT_PR_MODE)

    @field_validator("name", "repo_path", "test_command", "branch", "github_base_branch")
    @classmethod
    def required_string_must_not_be_blank(cls, value: str) -> str:
        if _is_blank(value):
            raise ValueError("Field must not be blank")
        return value

    @field_validator("pr_mode")
    @classmethod
    def pr_mode_must_be_allowed(cls, value: str) -> str:
        return normalize_pr_mode(value)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)
    test_command: Optional[str] = Field(default=None, min_length=1, max_length=PROJECT_TEST_COMMAND_MAX_LENGTH)
    branch: Optional[str] = Field(default=None, min_length=1, max_length=GITHUB_BASE_BRANCH_MAX_LENGTH)
    description: Optional[str] = Field(default=None, max_length=PROJECT_DESCRIPTION_MAX_LENGTH)
    github_token: Optional[str] = Field(default=None, max_length=GITHUB_TOKEN_MAX_LENGTH)
    github_owner: Optional[str] = Field(default=None, max_length=GITHUB_OWNER_MAX_LENGTH)
    github_repo: Optional[str] = Field(default=None, max_length=GITHUB_REPO_MAX_LENGTH)
    github_base_branch: Optional[str] = Field(default=None, min_length=1, max_length=GITHUB_BASE_BRANCH_MAX_LENGTH)
    pr_mode: Optional[str] = Field(default=None)

    @field_validator("name", "test_command", "branch", "github_base_branch")
    @classmethod
    def provided_string_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if _is_blank(value):
            raise ValueError("Field must not be blank")
        return value

    @field_validator("pr_mode")
    @classmethod
    def pr_mode_must_be_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return normalize_pr_mode(value)


class ProjectDetectRequest(BaseModel):
    repo_path: str = Field(min_length=1, max_length=PROJECT_REPO_PATH_MAX_LENGTH)

    @field_validator("repo_path")
    @classmethod
    def repo_path_must_not_be_blank(cls, value: str) -> str:
        if _is_blank(value):
            raise ValueError("Field must not be blank")
        return value


class ProjectDetectResponse(BaseModel):
    repo_path_input: str
    is_git_repo: bool = False
    git_root: Optional[str] = None
    path_is_git_root: bool = False
    current_branch: Optional[str] = None
    origin_url: Optional[str] = None
    is_github_remote: bool = False
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    gh_installed: bool = False
    gh_authenticated: bool = False
    recommended_pr_mode: str = DEFAULT_PR_MODE
    # Detect-and-prefill only (Phase 0, G4): a suggested test command for the
    # New Project form, or None when no confident signal was found. Never
    # executed and never auto-saved; the human confirms or edits it.
    suggested_test_command: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class ProjectResponse(BaseModel):
    id: str
    name: str
    repo_path: str
    test_command: str
    branch: str = "main"
    description: str = ""
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    github_base_branch: str = "pipewright-staging"
    pr_mode: str = DEFAULT_PR_MODE
    has_github_token: bool = False
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
