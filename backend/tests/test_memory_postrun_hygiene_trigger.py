"""
test_memory_postrun_hygiene_trigger.py
Dormant Row 16 PR-A post-run memory hygiene trigger tests.
"""

import importlib
import inspect
import uuid

import pytest
from sqlalchemy import text

from backend.core.statuses import RunStatus
from backend.db.database import engine
from backend.memory.bootstrap import list_suggestions
from backend.memory.memory_store import list_facts
from backend.memory.prompt_builder import build_project_memory_block
from backend.pipeline import chunked_orchestrator, policy, pr_orchestrator
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def reload_postrun_policy(monkeypatch):
    def reload_with(value: str | None):
        if value is None:
            monkeypatch.delenv(policy.MEMORY_POSTRUN_HYGIENE_ENV, raising=False)
        else:
            monkeypatch.setenv(policy.MEMORY_POSTRUN_HYGIENE_ENV, value)
        return importlib.reload(policy)

    yield reload_with

    monkeypatch.delenv(policy.MEMORY_POSTRUN_HYGIENE_ENV, raising=False)
    importlib.reload(policy)


@pytest.fixture()
def run_env(tmp_repo):
    project_ids: list[str] = []
    run_ids: list[str] = []

    def add_project(
        *,
        pr_mode: str = "local_only",
        test_command: str = "python -m pytest -q",
    ) -> dict:
        project = create_project(
            name=f"Postrun Hygiene {uuid.uuid4()}",
            repo_path=str(tmp_repo),
            test_command=test_command,
            github_base_branch="pipewright-staging",
            pr_mode=pr_mode,
        )
        project_ids.append(project["id"])
        return project

    def add_run(project_id: str, *, status: str = RunStatus.FINAL_APPROVED) -> str:
        run_id = str(uuid.uuid4())
        run_ids.append(run_id)
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_runs
                    (id, project_id, feature_description, status, chunk_plan_status)
                VALUES
                    (:id, :project_id, :feature_description, :status, 'approved')
            """), {
                "id": run_id,
                "project_id": project_id,
                "feature_description": "Post-run hygiene trigger test",
                "status": status,
            })
        return run_id

    env = type("RunEnv", (), {})()
    env.add_project = add_project
    env.add_run = add_run
    yield env

    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM chunks WHERE run_id = :run_id"), {
                "run_id": run_id,
            })
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :run_id"), {
                "run_id": run_id,
            })
        for project_id in project_ids:
            conn.execute(text(
                "DELETE FROM memory_suggestions WHERE project_id = :project_id"
            ), {"project_id": project_id})
            conn.execute(text(
                "DELETE FROM memory_facts WHERE project_id = :project_id"
            ), {"project_id": project_id})
            conn.execute(text("DELETE FROM projects WHERE id = :project_id"), {
                "project_id": project_id,
            })


def _read_run_status(run_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status
            FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    return row[0] if row else None


def _count_approval_gates(run_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("""
            SELECT COUNT(*)
            FROM approval_gates
            WHERE run_id = :run_id
        """), {"run_id": run_id}).scalar_one())


def _patch_local_only_git(monkeypatch) -> None:
    monkeypatch.setattr(pr_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "branch_exists",
        lambda branch, repo: True,
    )


def _patch_github_cli_success(monkeypatch, branch_name: str) -> None:
    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        pr_orchestrator,
        "ensure_remote_base_branch",
        lambda repo_path, base_branch, remote: None,
    )
    monkeypatch.setattr(pr_orchestrator.gh_pr, "ensure_gh_ready", lambda: None)
    monkeypatch.setattr(pr_orchestrator.local_git, "ensure_git_repo", lambda repo: None)
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "branch_exists",
        lambda branch, repo: True,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "get_current_branch",
        lambda repo: branch_name,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "ensure_clean_worktree",
        lambda repo: None,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "commits_ahead",
        lambda base, branch, repo: 1,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "branch_exists_remote",
        lambda repo, branch, remote="origin": True,
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "run_git",
        lambda args, repo: Result(),
    )
    monkeypatch.setattr(
        pr_orchestrator.local_git,
        "push_branch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("push_branch must not be called when remote branch exists")
        ),
    )
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "find_open_pr",
        lambda repo, branch, base: {
            "url": "https://github.com/acme/demo/pull/77",
            "number": 77,
            "title": "Existing PR",
        },
    )
    monkeypatch.setattr(
        pr_orchestrator.gh_pr,
        "create_pr",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("create_pr must not be called when a PR exists")
        ),
    )


def test_postrun_hygiene_flag_defaults_off(reload_postrun_policy):
    reloaded = reload_postrun_policy(None)

    assert reloaded.MEMORY_POSTRUN_HYGIENE_ENABLED is False


def test_postrun_hygiene_env_unset_defaults_false(reload_postrun_policy):
    reloaded = reload_postrun_policy(None)

    assert reloaded.MEMORY_POSTRUN_HYGIENE_ENABLED is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", " On "])
def test_postrun_hygiene_env_truthy_values_enable_flag(
    reload_postrun_policy,
    value,
):
    reloaded = reload_postrun_policy(value)

    assert reloaded.MEMORY_POSTRUN_HYGIENE_ENABLED is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "maybe", "treu"])
def test_postrun_hygiene_env_falsey_or_invalid_values_disable_flag(
    reload_postrun_policy,
    value,
):
    reloaded = reload_postrun_policy(value)

    assert reloaded.MEMORY_POSTRUN_HYGIENE_ENABLED is False


def test_env_false_successful_local_only_complete_does_not_call_generator(
    monkeypatch,
    reload_postrun_policy,
    run_env,
):
    reload_postrun_policy("false")
    project = run_env.add_project(pr_mode="local_only")
    run_id = run_env.add_run(project["id"])
    calls: list[tuple[tuple, dict]] = []
    _patch_local_only_git(monkeypatch)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == RunStatus.COMPLETE
    assert calls == []
    assert list_suggestions(project["id"], status="pending") == []


def test_env_invalid_successful_local_only_complete_does_not_call_generator(
    monkeypatch,
    reload_postrun_policy,
    run_env,
):
    reload_postrun_policy("definitely")
    project = run_env.add_project(pr_mode="local_only")
    run_id = run_env.add_run(project["id"])
    calls: list[tuple[tuple, dict]] = []
    _patch_local_only_git(monkeypatch)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == RunStatus.COMPLETE
    assert calls == []
    assert list_suggestions(project["id"], status="pending") == []


def test_env_true_success_trigger_creates_pending_only_suggestions(
    monkeypatch,
    reload_postrun_policy,
    run_env,
):
    reload_postrun_policy("true")
    project = run_env.add_project(
        pr_mode="local_only",
        test_command="python -m pytest backend/tests -q",
    )
    run_id = run_env.add_run(project["id"])
    _patch_local_only_git(monkeypatch)
    before_block = build_project_memory_block(project["id"], role="coder")

    result = pr_orchestrator.push_and_create_pr(run_id)
    after_block = build_project_memory_block(project["id"], role="coder")

    suggestions = [
        suggestion
        for suggestion in list_suggestions(project["id"], status="pending")
        if suggestion["source_run_id"] == run_id
    ]
    assert result["status"] == RunStatus.COMPLETE
    assert len(suggestions) == 1
    assert suggestions[0]["content"] == (
        "Project test command: python -m pytest backend/tests -q"
    )
    assert suggestions[0]["status"] == "pending"
    assert suggestions[0]["suggested_by"] == "postrun_auto"
    assert list_facts(project["id"], status="active") == []
    assert _count_approval_gates(run_id) == 0
    assert after_block == before_block == ""


def test_flag_off_successful_local_only_complete_does_not_call_generator(
    monkeypatch,
    run_env,
):
    project = run_env.add_project(pr_mode="local_only")
    run_id = run_env.add_run(project["id"])
    _patch_local_only_git(monkeypatch)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generator must not run while flag is off")
        ),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == RunStatus.COMPLETE
    assert _read_run_status(run_id) == RunStatus.COMPLETE
    assert list_suggestions(project["id"], status="pending") == []


def test_flag_on_local_only_success_calls_generator_once_with_auto_provenance(
    monkeypatch,
    run_env,
):
    project = run_env.add_project(pr_mode="local_only")
    run_id = run_env.add_run(project["id"])
    calls: list[tuple[str, str | None]] = []
    _patch_local_only_git(monkeypatch)
    monkeypatch.setattr(policy, "MEMORY_POSTRUN_HYGIENE_ENABLED", True)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda run_id, *, requested_by: calls.append((run_id, requested_by)),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == RunStatus.COMPLETE
    assert calls == [(run_id, "postrun_auto")]


def test_flag_on_github_cli_success_calls_generator_once_with_auto_provenance(
    monkeypatch,
    run_env,
):
    project = run_env.add_project(pr_mode="github_cli")
    run_id = run_env.add_run(project["id"])
    branch_name = f"pipewright/{run_id[:8]}"
    calls: list[tuple[str, str | None]] = []
    _patch_github_cli_success(monkeypatch, branch_name)
    monkeypatch.setattr(policy, "MEMORY_POSTRUN_HYGIENE_ENABLED", True)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda run_id, *, requested_by: calls.append((run_id, requested_by)),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == RunStatus.COMPLETE
    assert result["pr_number"] == 77
    assert calls == [(run_id, "postrun_auto")]


def test_generator_exception_does_not_break_successful_completion(
    monkeypatch,
    run_env,
):
    project = run_env.add_project(pr_mode="local_only")
    run_id = run_env.add_run(project["id"])
    _patch_local_only_git(monkeypatch)
    monkeypatch.setattr(policy, "MEMORY_POSTRUN_HYGIENE_ENABLED", True)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = pr_orchestrator.push_and_create_pr(run_id)

    assert result["status"] == RunStatus.COMPLETE
    assert _read_run_status(run_id) == RunStatus.COMPLETE


def test_postrun_hygiene_does_not_fire_on_push_failed(
    monkeypatch,
    run_env,
):
    project = run_env.add_project(pr_mode="github_cli")
    run_id = run_env.add_run(project["id"])
    calls: list[str] = []
    monkeypatch.setattr(policy, "MEMORY_POSTRUN_HYGIENE_ENABLED", True)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda run_id, **kwargs: calls.append(run_id),
    )
    monkeypatch.setattr(
        pr_orchestrator,
        "ensure_remote_base_branch",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("missing base")),
    )

    with pytest.raises(RuntimeError, match="push-pr failed"):
        pr_orchestrator.push_and_create_pr(run_id)

    assert calls == []
    assert _read_run_status(run_id) == RunStatus.PUSH_FAILED


def test_postrun_hygiene_is_not_attached_to_update_run_status():
    source = inspect.getsource(chunked_orchestrator._update_run_status)

    assert "postrun" not in source
    assert "generate_run_memory_suggestions" not in source


def test_missing_project_id_short_circuits_without_generator(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(policy, "MEMORY_POSTRUN_HYGIENE_ENABLED", True)
    monkeypatch.setattr(
        pr_orchestrator,
        "generate_run_memory_suggestions",
        lambda run_id, **kwargs: calls.append(run_id),
    )

    pr_orchestrator._maybe_generate_postrun_memory_suggestions("run-without-project", None)

    assert calls == []


def test_real_generator_remains_pending_only_and_idempotent(monkeypatch, run_env):
    project = run_env.add_project(
        pr_mode="local_only",
        test_command="python -m pytest backend/tests -q",
    )
    run_id = run_env.add_run(project["id"], status=RunStatus.COMPLETE)
    monkeypatch.setattr(policy, "MEMORY_POSTRUN_HYGIENE_ENABLED", True)
    before_block = build_project_memory_block(project["id"], role="coder")

    pr_orchestrator._maybe_generate_postrun_memory_suggestions(run_id, project["id"])
    pr_orchestrator._maybe_generate_postrun_memory_suggestions(run_id, project["id"])
    after_block = build_project_memory_block(project["id"], role="coder")

    suggestions = [
        suggestion
        for suggestion in list_suggestions(project["id"], status="pending")
        if suggestion["source_run_id"] == run_id
    ]
    assert len(suggestions) == 1
    assert suggestions[0]["content"] == (
        "Project test command: python -m pytest backend/tests -q"
    )
    assert suggestions[0]["status"] == "pending"
    assert suggestions[0]["suggested_by"] == "postrun_auto"
    assert list_facts(project["id"], status="active") == []
    assert _count_approval_gates(run_id) == 0
    assert after_block == before_block == ""
