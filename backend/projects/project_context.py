"""
project_context.py
Runtime project configuration for a pipeline run.
Lets stages resolve repo path and test command from the selected project.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from contextlib import contextmanager

from backend.config.keys import settings


@dataclass(frozen=True)
class ProjectRuntimeConfig:
    project_id: str
    repo_path: str
    test_command: str


_active_project: ContextVar[ProjectRuntimeConfig | None] = ContextVar(
    "active_project",
    default=None
)


@contextmanager
def active_project(project: ProjectRuntimeConfig):
    token = _active_project.set(project)
    try:
        yield
    finally:
        _active_project.reset(token)


def get_active_project() -> ProjectRuntimeConfig | None:
    return _active_project.get()


def get_target_repo_path() -> str:
    project = get_active_project()
    if project:
        return project.repo_path
    if settings.target_repo_path:
        return settings.target_repo_path
    raise RuntimeError("project_context.py: no target repo path configured")


def get_test_command() -> str:
    project = get_active_project()
    if project:
        return project.test_command
    if settings.test_command:
        return settings.test_command
    raise RuntimeError("project_context.py: no test command configured")
