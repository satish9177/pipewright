"""
run_locks.py
Lightweight in-process project/repo operation locks.
"""

from contextlib import asynccontextmanager, contextmanager
from threading import Lock


PROJECT_LOCK_CONFLICT_MESSAGE = (
    "Project is already running a repo operation. "
    "Try again after the current operation finishes."
)


class ProjectRepoLockError(RuntimeError):
    pass


_registry_lock = Lock()
_locked_projects: set[str] = set()


def _acquire_project_lock(project_id: str) -> None:
    if not project_id:
        raise ValueError("run_locks.py: project_id is required")
    with _registry_lock:
        if project_id in _locked_projects:
            raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)
        _locked_projects.add(project_id)


def _release_project_lock(project_id: str) -> None:
    with _registry_lock:
        _locked_projects.discard(project_id)


@asynccontextmanager
async def project_repo_lock(project_id: str):
    _acquire_project_lock(project_id)
    try:
        yield
    finally:
        _release_project_lock(project_id)


@contextmanager
def project_repo_lock_sync(project_id: str):
    _acquire_project_lock(project_id)
    try:
        yield
    finally:
        _release_project_lock(project_id)


def is_project_locked(project_id: str) -> bool:
    with _registry_lock:
        return project_id in _locked_projects
