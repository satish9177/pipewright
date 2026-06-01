"""
test_file_alias_grounding.py
Unit tests for the deterministic explicit-edit-target resolver (PR #17B).

No LLM, no filesystem walk, no file-content reads. Tests seed a project's
file_index directly (same pattern as test_plan_path_grounding.py) and call the
helper in isolation.

Root-cause note: the dogfood bug ("add hello in the readme" rejected as too
vague) is a *route-level* symptom. Reproducing the route rejection requires
wiring the resolver into /runs/chunked, which is PR #17C, not #17B. Here we
assert the corrected resolver expectation directly:
    resolve_explicit_edit_target(pid, "add hello in the readme")
        == GROUNDED("README.md")   # when README.md is indexed
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.pipeline.file_alias_grounding import (
    EditTargetOutcome,
    resolve_explicit_edit_target,
)

pytestmark = pytest.mark.unit


def _seed_index(project_id: str, paths: list[str]) -> None:
    with engine.begin() as conn:
        for path in paths:
            conn.execute(text("""
                INSERT INTO file_index
                (id, project_id, path, file_type, summary, key_imports,
                 last_modified, token_estimate, line_count, size_bytes)
                VALUES
                (:id, :project_id, :path, 'unknown', NULL, '[]',
                 NULL, 100, 10, 100)
            """), {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "path": path,
            })


def _project(paths: list[str]) -> str:
    """Create an isolated project_id with the given indexed paths."""
    project_id = f"alias-{uuid.uuid4()}"
    _seed_index(project_id, paths)
    return project_id


@pytest.fixture(autouse=True)
def _cleanup_index():
    """Remove any alias-* file_index rows created during a test."""
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM file_index WHERE project_id LIKE 'alias-%'"))


# --------------------------------------------------------------------------
# readme alias
# --------------------------------------------------------------------------

def test_readme_alias_grounds_to_indexed_readme():
    pid = _project(["README.md", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "add hello in the readme")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "README.md"


def test_readme_alias_not_found_when_no_readme_indexed():
    pid = _project(["backend/app.py", "frontend/src/App.tsx"])
    result = resolve_explicit_edit_target(pid, "add hello in the readme")
    assert result.outcome is EditTargetOutcome.NOT_FOUND
    assert result.alias == "readme"


def test_readme_alias_ambiguous_when_multiple_readmes():
    pid = _project(["README.md", "docs/README.md", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "add hello in the readme")
    assert result.outcome is EditTargetOutcome.AMBIGUOUS
    assert result.alias == "readme"
    # Deterministically sorted.
    assert result.candidates == ("README.md", "docs/README.md")


def test_readme_alias_is_case_insensitive():
    pid = _project(["README.md"])
    result = resolve_explicit_edit_target(pid, "add hello in the README")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "README.md"


# --------------------------------------------------------------------------
# explicit relative paths
# --------------------------------------------------------------------------

def test_explicit_readme_path_grounds():
    pid = _project(["README.md"])
    result = resolve_explicit_edit_target(pid, "add hello bro to README.md")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "README.md"


def test_explicit_nested_path_grounds():
    pid = _project(["docs/usage.md", "README.md"])
    result = resolve_explicit_edit_target(pid, "append test text to docs/usage.md")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "docs/usage.md"


def test_explicit_nested_path_not_found_when_missing():
    pid = _project(["README.md", "docs/intro.md"])
    result = resolve_explicit_edit_target(pid, "append test text to docs/usage.md")
    assert result.outcome is EditTargetOutcome.NOT_FOUND
    assert result.alias == "docs/usage.md"


def test_explicit_path_trailing_period_is_stripped():
    pid = _project(["docs/usage.md"])
    result = resolve_explicit_edit_target(pid, "edit docs/usage.md.")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "docs/usage.md"


# --------------------------------------------------------------------------
# package.json / docker compose / requirements / pyproject aliases
# --------------------------------------------------------------------------

def test_package_json_alias_grounds():
    pid = _project(["package.json", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "update package json script name")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "package.json"


def test_docker_compose_alias_grounds():
    pid = _project(["docker-compose.yml", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "add note to docker compose comment")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "docker-compose.yml"


def test_docker_compose_alias_hyphenated_trigger():
    pid = _project(["docker-compose.yml"])
    result = resolve_explicit_edit_target(pid, "add note to docker-compose comment")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "docker-compose.yml"


def test_docker_compose_alias_ambiguous():
    pid = _project(["docker-compose.yml", "compose.yaml"])
    result = resolve_explicit_edit_target(pid, "add note to docker compose")
    assert result.outcome is EditTargetOutcome.AMBIGUOUS
    assert result.alias == "docker compose"
    assert result.candidates == ("compose.yaml", "docker-compose.yml")


def test_requirements_alias_grounds():
    pid = _project(["requirements.txt", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "add a package to requirements")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "requirements.txt"


def test_requirements_alias_ambiguous():
    pid = _project(["requirements.txt", "requirements-dev.txt"])
    result = resolve_explicit_edit_target(pid, "add a package to requirements")
    assert result.outcome is EditTargetOutcome.AMBIGUOUS
    assert result.alias == "requirements"
    assert result.candidates == ("requirements-dev.txt", "requirements.txt")


def test_pyproject_alias_grounds():
    pid = _project(["pyproject.toml", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "update pyproject dependency")
    assert result.outcome is EditTargetOutcome.GROUNDED
    assert result.path == "pyproject.toml"


# --------------------------------------------------------------------------
# no target (generic + excluded entity aliases)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("feature", [
    "fix it",
    "make backend better",
    "add comment in user model",
    "improve the login button",
    "refactor the auth service",
])
def test_generic_and_entity_requests_have_no_target(feature):
    pid = _project(["backend/models/user.py", "frontend/src/Login.tsx"])
    result = resolve_explicit_edit_target(pid, feature)
    assert result.outcome is EditTargetOutcome.NO_TARGET


def test_non_path_slash_token_is_not_a_target():
    # "and/or" must not be mistaken for a file path.
    pid = _project(["backend/app.py"])
    result = resolve_explicit_edit_target(pid, "handle the and/or case")
    assert result.outcome is EditTargetOutcome.NO_TARGET


# --------------------------------------------------------------------------
# forbidden targets
# --------------------------------------------------------------------------

def test_env_is_forbidden_even_if_indexed():
    # .env would never normally be indexed, but even if it is, never ground it.
    pid = _project([".env", "backend/app.py"])
    result = resolve_explicit_edit_target(pid, "add a key to .env")
    assert result.outcome is EditTargetOutcome.FORBIDDEN
    assert result.alias == ".env"


def test_env_local_is_forbidden():
    pid = _project(["backend/app.py"])
    result = resolve_explicit_edit_target(pid, "update .env.local secret")
    assert result.outcome is EditTargetOutcome.FORBIDDEN
    assert result.alias == ".env.local"


def test_git_internal_is_forbidden():
    pid = _project(["backend/app.py"])
    result = resolve_explicit_edit_target(pid, "edit .git/config remote url")
    assert result.outcome is EditTargetOutcome.FORBIDDEN
    assert result.alias == ".git/config"


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------

def test_repeated_calls_are_deterministic():
    pid = _project(["README.md", "docs/README.md"])
    results = [
        resolve_explicit_edit_target(pid, "add hello in the readme")
        for _ in range(3)
    ]
    assert all(r == results[0] for r in results)
    assert results[0].outcome is EditTargetOutcome.AMBIGUOUS
    assert results[0].candidates == ("README.md", "docs/README.md")
