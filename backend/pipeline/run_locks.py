"""
run_locks.py
Lightweight in-process project/repo operation locks.
"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from threading import Lock


PROJECT_LOCK_CONFLICT_MESSAGE = "A run is already active for this project."


class ProjectRepoLockError(RuntimeError):
    pass


_registry_lock = Lock()
_project_locks: dict[str, asyncio.Lock] = {}
_locked_projects: set[str] = set()


def get_project_lock(project_id: str) -> asyncio.Lock:
    if not project_id:
        raise ValueError("run_locks.py: project_id is required")
    with _registry_lock:
        lock = _project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _project_locks[project_id] = lock
        return lock


def _acquire_project_lock(project_id: str) -> None:
    if not project_id:
        raise ValueError("run_locks.py: project_id is required")
    with _registry_lock:
        lock = _project_locks.get(project_id)
        if project_id in _locked_projects or (lock is not None and lock.locked()):
            raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)
        _locked_projects.add(project_id)


def _release_project_lock(project_id: str) -> None:
    with _registry_lock:
        _locked_projects.discard(project_id)


@asynccontextmanager
async def project_repo_lock(project_id: str):
    lock = get_project_lock(project_id)
    with _registry_lock:
        if project_id in _locked_projects or lock.locked():
            raise ProjectRepoLockError(PROJECT_LOCK_CONFLICT_MESSAGE)
        _locked_projects.add(project_id)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
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
        lock = _project_locks.get(project_id)
        return project_id in _locked_projects or (
            lock is not None and lock.locked()
        )
