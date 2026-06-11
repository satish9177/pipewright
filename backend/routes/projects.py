"""
projects.py
Project CRUD routes.
"""

import logging

from fastapi import APIRouter, HTTPException

from backend.git.repo_inspect import detect_repo
from backend.models.handoff import (
    ProjectCreate,
    ProjectDetectRequest,
    ProjectDetectResponse,
    ProjectUpdate,
)
from backend.pipeline.test_command_detection import detect_test_command
from backend.pipeline.run_locks import (
    ProjectRepoLockError,
    project_repo_lock_sync,
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
from backend.repo.index_freshness import (
    get_project_index_freshness,
    reindex_and_record,
)
from backend.repo.repo_indexer import get_project_index_status

logger = logging.getLogger(__name__)

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

    Inspects the repo at repo_path and recommends a pr_mode, and suggests a
    test command (detect-and-prefill only). This endpoint never saves project
    settings and never mutates git state: it does not create branches, push,
    create PRs, or execute the suggested test command.
    """
    try:
        detection = detect_repo(request.repo_path)
        # Detect-and-prefill only: suggest a test command from repo markers for
        # the New Project form. Pure/read-only (no subprocess, no execution);
        # returns None when no confident signal is found. The endpoint persists
        # nothing, so this can never overwrite a configured command.
        detection["suggested_test_command"] = detect_test_command(request.repo_path)
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


@router.get("/projects/{project_id}/index")
def get_project_index_route(project_id: str):
    """
    Return read-only repo index status for a project (#19B).

    Reads only the file_index rows (count + most recent indexed_at). Never
    scans the repo and never calls build_repo_index.
    """
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return get_project_index_status(project_id)


@router.get("/projects/{project_id}/index/freshness")
def get_project_index_freshness_route(project_id: str):
    """
    Return live repo-index freshness status for a project (#34C).

    This endpoint may run cheap Git subprocess checks. Keep
    GET /projects/{project_id}/index as the pure DB count/age endpoint.
    """
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return get_project_index_freshness(project_id, project.get("repo_path"))


@router.post("/projects/{project_id}/reindex")
def reindex_project_route(project_id: str):
    """
    Force a fresh repo index rebuild for a project (#19B).

    Read-only on the target repo: it scans the working tree as-is and atomically
    replaces the project's file_index rows via build_repo_index. It never stages,
    commits, patches, pushes, switches branches, or writes to the repo, and it
    does NOT require a clean working tree.

    Lock-aware: the existing project repo lock is acquired for the scan duration
    so a run cannot start mid-scan; if a run already holds the lock, this returns
    HTTP 409 and build_repo_index is never called.
    """
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    repo_path = project.get("repo_path")
    try:
        with project_repo_lock_sync(project_id):
            record_result = reindex_and_record(project_id, repo_path)
            status = get_project_index_status(project_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except HTTPException:
        raise
    except Exception as error:
        # Never leak raw stack traces. Log the detail server-side; return a
        # sanitized, route-style message to the client.
        logger.warning(
            "[PROJECTS] Re-index failed | project_id=%s | error=%s",
            project_id,
            error,
        )
        raise HTTPException(
            status_code=400,
            detail="Re-index failed. Check that the project repo path exists "
            "and is readable.",
        )

    files_indexed = status["files_indexed"]
    return {
        "status": "complete",
        "project_id": project_id,
        "target_repo_path": repo_path,
        "files_indexed": files_indexed,
        "indexed_at": status["indexed_at"],
        "freshness": {
            "state": record_result.state.value,
            "reasons": list(record_result.reasons),
        },
        "message": f"Re-indexed {files_indexed} files.",
    }
