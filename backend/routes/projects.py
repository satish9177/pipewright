"""
projects.py
Project CRUD routes.
"""

from fastapi import APIRouter, HTTPException

from backend.git.repo_inspect import detect_repo
from backend.models.handoff import (
    ProjectCreate,
    ProjectDetectRequest,
    ProjectDetectResponse,
    ProjectUpdate,
)
from backend.projects.project_responses import (
    sanitize_project_list_response,
    sanitize_project_response,
)
from backend.projects.project_store import (
    create_project,
    get_project,
    list_projects as list_stored_projects,
    update_project,
)

router = APIRouter()


@router.post("/projects")
def create_project_route(request: ProjectCreate):
    try:
        project = create_project(
            name=request.name,
            repo_path=request.repo_path,
            test_command=request.test_command,
            branch=request.branch,
            description=request.description,
            github_token=request.github_token,
            github_owner=request.github_owner,
            github_repo=request.github_repo,
            github_base_branch=request.github_base_branch,
            pr_mode=request.pr_mode,
        )
        return sanitize_project_response(project)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/projects/detect", response_model=ProjectDetectResponse)
def detect_project_route(request: ProjectDetectRequest):
    """
    Read-only detection for the New Project flow.

    Inspects the repo at repo_path and recommends a pr_mode. This endpoint
    never saves project settings and never mutates git state: it does not
    create branches, push, or create PRs.
    """
    try:
        detection = detect_repo(request.repo_path)
        return ProjectDetectResponse(**detection)
    except Exception as error:
        # Detection must never leak raw errors to the frontend.
        raise HTTPException(
            status_code=500,
            detail=f"Project detection failed: {error}",
        )


@router.get("/projects")
def list_projects_route():
    return sanitize_project_list_response(list_stored_projects())


@router.get("/projects/{project_id}")
def get_project_route(project_id: str):
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return sanitize_project_response(project)


@router.patch("/projects/{project_id}")
def update_project_endpoint(
    project_id: str,
    request: ProjectUpdate,
):
    """
    Update project fields.
    Use this to add GitHub credentials to a project.
    Only fields provided are updated.
    Fields not provided keep their existing values.
    """
    try:
        project = update_project(
            project_id=project_id,
            name=request.name,
            test_command=request.test_command,
            branch=request.branch,
            description=request.description,
            github_token=request.github_token,
            github_owner=request.github_owner,
            github_repo=request.github_repo,
            github_base_branch=request.github_base_branch,
            pr_mode=request.pr_mode,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))
    if not project:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )
    return sanitize_project_response(project)
