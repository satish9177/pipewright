"""
pr_orchestrator.py
Phase 2B-4D final push and GitHub PR creation.

This module only runs after final human approval. It does not merge PRs,
delete branches, force push, poll CI, or create per-chunk PRs.
"""

from datetime import datetime, timezone

from github import Github
from sqlalchemy import text

from backend.core.statuses import RunStatus
from backend.db.database import engine, init_db
from backend.git import local_git
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.run_locks import project_repo_lock_sync
from backend.projects.project_store import require_project
from backend.security.secrets import decrypt_secret


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_run(run_id: str) -> dict:
    init_db()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    if row is None:
        raise ValueError(f"pr_orchestrator.py: run not found: {run_id}")
    return dict(row._mapping)


def _mark_pushing(run_id: str, branch_name: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = 'push_pr',
                branch_name = :branch_name,
                push_error = NULL
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "branch_name": branch_name,
            "status": RunStatus.PUSHING,
        })


def _mark_push_failed(run_id: str, error: str, branch_name: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = 'push_pr_failed',
                branch_name = COALESCE(:branch_name, branch_name),
                push_error = :push_error
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "branch_name": branch_name,
            "push_error": error,
            "status": RunStatus.PUSH_FAILED,
        })


def _save_pushed(run_id: str, branch_name: str) -> str:
    pushed_at = _utc_now()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET branch_name = :branch_name,
                pushed_at = COALESCE(pushed_at, :pushed_at)
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "branch_name": branch_name,
            "pushed_at": pushed_at,
        })
    return pushed_at


def _save_pr_metadata(
    run_id: str,
    branch_name: str,
    pr_url: str,
    pr_number: int,
) -> dict:
    pr_created_at = _utc_now()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = :current_step,
                pr_url = :pr_url,
                pr_number = :pr_number,
                branch_name = :branch_name,
                pr_created_at = COALESCE(pr_created_at, :pr_created_at),
                push_error = NULL
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "branch_name": branch_name,
            "pr_created_at": pr_created_at,
            "status": RunStatus.COMPLETE,
            "current_step": RunStatus.COMPLETE,
        })
        if result.rowcount == 0:
            raise ValueError(f"pr_orchestrator.py: run not found: {run_id}")
    return {
        "status": RunStatus.COMPLETE,
        "run_id": run_id,
        "branch_name": branch_name,
        "pr_url": pr_url,
        "pr_number": pr_number,
    }


def _truncate(value: str | None, limit: int = 500) -> str:
    if not value:
        return "Summary: not available"
    text_value = str(value)
    if len(text_value) <= limit:
        return text_value
    return text_value[:limit] + "... [truncated]"


def build_pr_body(run_id: str) -> str:
    run = _load_run(run_id)
    branch_name = run.get("branch_name") or f"pipewright/{run_id[:8]}"
    plan_status = get_chunk_plan_status(run_id)
    lines = [
        "## Pipewright Run",
        "",
        "Feature:",
        run.get("feature_description") or "",
        "",
        "Run:",
        run_id,
        "",
        "Branch:",
        branch_name,
        "",
        "## Chunks",
        "",
    ]
    for chunk in plan_status.chunks:
        lines.append(f"{chunk.chunk_number}. {chunk.title} - {chunk.status}")
        lines.append(_truncate(chunk.completion_summary))
        lines.append("")
    lines.extend([
        "---",
        "Created by Pipewright.",
    ])
    return "\n".join(lines)[:10000]


def _remote_matches(remote_url: str, owner: str, repo_name: str) -> bool:
    normalized = remote_url.strip().lower()
    expected = f"{owner}/{repo_name}".lower().removesuffix(".git")
    normalized = normalized.removesuffix(".git")
    if normalized.startswith("git@github.com:"):
        path = normalized.split("git@github.com:", 1)[1]
        return path == expected
    if "github.com/" in normalized:
        path = normalized.split("github.com/", 1)[1]
        return path == expected
    return False


def _verify_local_branch(repo_path: str, branch_name: str, owner: str, repo_name: str) -> None:
    local_git.ensure_git_repo(repo_path)
    if not local_git.branch_exists(branch_name, repo_path):
        raise RuntimeError(
            f"pr_orchestrator.py: expected local branch missing: {branch_name}"
        )

    current_branch = local_git.get_current_branch(repo_path)
    if current_branch != branch_name:
        result = local_git.run_git(["checkout", branch_name], repo_path)
        if result.returncode != 0:
            raise RuntimeError(
                f"pr_orchestrator.py: failed to checkout {branch_name}: "
                f"{result.stderr.strip()}"
            )

    local_git.ensure_clean_worktree(repo_path)
    remote_url = local_git.get_remote_url(repo_path)
    if not _remote_matches(remote_url, owner, repo_name):
        raise RuntimeError(
            f"pr_orchestrator.py: origin remote does not match configured "
            f"GitHub repo {owner}/{repo_name}"
        )


def _require_project_github(project: dict) -> tuple[str, str, str, str]:
    stored_token = project.get("github_token")
    owner = project.get("github_owner")
    repo_name = project.get("github_repo")
    base_branch = project.get("github_base_branch") or "main"
    missing = [
        name for name, value in [
            ("github_token", stored_token),
            ("github_owner", owner),
            ("github_repo", repo_name),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"pr_orchestrator.py: missing GitHub project fields: "
            f"{', '.join(missing)}"
        )
    token = decrypt_secret(stored_token)
    return token, owner, repo_name, base_branch


def _get_github_repo(token: str, owner: str, repo_name: str):
    try:
        client = Github(token)
        client.get_user().login
        return client.get_repo(f"{owner}/{repo_name}")
    except Exception as error:
        raise RuntimeError(f"pr_orchestrator.py: GitHub auth/repo failed: {error}")


def _find_existing_pr(repo, owner: str, branch_name: str, base_branch: str):
    pulls = repo.get_pulls(
        state="open",
        head=f"{owner}:{branch_name}",
        base=base_branch,
    )
    for pull in pulls:
        return pull
    return None


def _create_pr(repo, run_id: str, feature_description: str, branch_name: str, base_branch: str):
    title = feature_description.strip() if feature_description and feature_description.strip() else (
        f"Pipewright run {run_id[:8]}"
    )
    return repo.create_pull(
        title=title,
        body=build_pr_body(run_id),
        head=branch_name,
        base=base_branch,
    )


def _ensure_branch_has_commits_ahead(repo_path: str, branch_name: str, base_branch: str) -> None:
    ahead_count = local_git.commits_ahead(base_branch, branch_name, repo_path)
    if ahead_count <= 0:
        raise RuntimeError(
            "Branch has no commits ahead of base; cannot push or create PR."
        )


def _push_and_create_pr_locked(run_id: str, run: dict) -> dict:
    branch_name = f"pipewright/{run_id[:8]}"
    if run.get("pr_url"):
        return {
            "status": RunStatus.COMPLETE,
            "run_id": run_id,
            "branch_name": run.get("branch_name") or branch_name,
            "pr_url": run["pr_url"],
            "pr_number": run.get("pr_number"),
        }

    status = run.get("status")
    if status not in {RunStatus.FINAL_APPROVED, RunStatus.PUSH_FAILED}:
        raise RuntimeError(
            f"pr_orchestrator.py: run is not ready for push-pr. "
            f"run_id={run_id} | status={status}"
        )

    project = require_project(run.get("project_id"))
    repo_path = project["repo_path"]
    token, owner, repo_name, base_branch = _require_project_github(project)

    try:
        _mark_pushing(run_id, branch_name)
        _verify_local_branch(repo_path, branch_name, owner, repo_name)
        _ensure_branch_has_commits_ahead(repo_path, branch_name, base_branch)
        if not local_git.branch_exists_remote(repo_path, branch_name):
            local_git.push_branch(branch_name, repo_path)
        _save_pushed(run_id, branch_name)

        github_repo = _get_github_repo(token, owner, repo_name)
        pull = _find_existing_pr(github_repo, owner, branch_name, base_branch)
        if pull is None:
            pull = _create_pr(
                github_repo,
                run_id,
                run.get("feature_description") or "",
                branch_name,
                base_branch,
            )
        return _save_pr_metadata(
            run_id,
            branch_name,
            pull.html_url,
            int(pull.number),
        )
    except Exception as error:
        error_text = str(error)
        _mark_push_failed(run_id, error_text, branch_name)
        raise RuntimeError(
            f"pr_orchestrator.py: push-pr failed. "
            f"run_id={run_id} | error={error_text}"
        )


def push_and_create_pr(run_id: str) -> dict:
    run = _load_run(run_id)
    project_id = run.get("project_id")
    if not project_id:
        raise RuntimeError(
            f"pr_orchestrator.py: run is missing project_id. run_id={run_id}"
        )
    with project_repo_lock_sync(project_id):
        return _push_and_create_pr_locked(run_id, run)
