"""
projects.py
Project CRUD routes.
"""

from fastapi import APIRouter, HTTPException

from backend.models.handoff import ProjectCreate, ProjectUpdate
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
        )
        return sanitize_project_response(project)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


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
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )
    return sanitize_project_response(project)
