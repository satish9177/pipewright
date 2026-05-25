"""
handoff.py
Pydantic models for all inter-module handoff contracts.
Every model call produces one of these.
Every downstream module receives one of these.
Never use raw dicts between pipeline modules.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


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
    action: str  # create / modify / delete
    content: Optional[str] = None
    reason: str


class CoderHandoff(BaseModel):
    handoff_from: str = "coder"
    handoff_to: str = "patch_applier"
    run_id: str
    feature_description: str
    files_changed: List[FileChange]
    summary: str
    suggested_memory_entries: List[str] = Field(default_factory=list)


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
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
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
    reason: str


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
    name: str
    repo_path: str
    test_command: str
    branch: str = "main"
    description: str = ""
    github_token: Optional[str] = None
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    github_base_branch: str = "pipewright-staging"


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    test_command: Optional[str] = None
    branch: Optional[str] = None
    description: Optional[str] = None
    github_token: Optional[str] = None
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    github_base_branch: Optional[str] = None


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
    has_github_token: bool = False
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
