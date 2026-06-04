"""
pr_orchestrator.py
Phase 2B-4D final push and GitHub PR creation.

This module only runs after final human approval. It does not merge PRs,
delete branches, force push, poll CI, or create per-chunk PRs.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from github import Github
from sqlalchemy import text

from backend.core.statuses import RunStatus
from backend.db.database import engine, init_db
from backend.git import gh_pr, local_git
from backend.git.pr_preflight import ensure_remote_base_branch
from backend.github.branch_safety import validate_base_branch
from backend.llm.sanitize import sanitize_for_log
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.run_locks import project_repo_lock_sync
from backend.projects.pr_modes import (
    PR_MODE_GITHUB_CLI,
    PR_MODE_LOCAL_ONLY,
    PR_MODE_MANUAL_TOKEN,
    normalize_pr_mode,
)
from backend.projects.project_store import require_project
from backend.security.secrets import decrypt_secret

PUSH_PR_ELIGIBLE = "push_pr_ready"
PUSH_PR_EXISTING_PR = "pr_already_recorded"
PUSH_PR_LOCAL_ONLY_READY = "local_only_ready"
PUSH_PR_INELIGIBLE_STATUS = "run_not_ready_for_push_pr"


@dataclass(frozen=True)
class PushPrEligibilityDecision:
    """
    Pure status/mode eligibility decision for push/create-PR.

    This helper deliberately does not verify branches, dirty tree, remote base
    state, GitHub auth, or existing remote PRs. Those checks remain in the locked
    mutating path because they depend on live git/GitHub state and must be
    revalidated at execution time.
    """

    eligible: bool
    reason: str | None
    status_code: int | None
    pr_mode: str


def evaluate_push_pr_eligibility(
    *,
    run_status: str | None,
    pr_mode: str,
    has_pr_url: bool,
) -> PushPrEligibilityDecision:
    """
    Decide whether the run status and PR mode allow push/create-PR handling.

    Existing DB PR URLs stay idempotently eligible regardless of run status,
    matching push_and_create_pr's current behavior. local_only allows COMPLETE in
    addition to FINAL_APPROVED/PUSH_FAILED because the local completion path is
    idempotent after it marks the run complete. Remote PR modes allow only
    FINAL_APPROVED or PUSH_FAILED before live git/GitHub checks proceed.
    """
    normalized_mode = normalize_pr_mode(pr_mode)
    if has_pr_url:
        return PushPrEligibilityDecision(
            eligible=True,
            reason=PUSH_PR_EXISTING_PR,
            status_code=None,
            pr_mode=normalized_mode,
        )

    allowed = {RunStatus.FINAL_APPROVED, RunStatus.PUSH_FAILED}
    if normalized_mode == PR_MODE_LOCAL_ONLY:
        allowed = allowed | {RunStatus.COMPLETE}
        if run_status in allowed:
            return PushPrEligibilityDecision(
                eligible=True,
                reason=PUSH_PR_LOCAL_ONLY_READY,
                status_code=None,
                pr_mode=normalized_mode,
            )
    elif run_status in allowed:
        return PushPrEligibilityDecision(
            eligible=True,
            reason=PUSH_PR_ELIGIBLE,
            status_code=None,
            pr_mode=normalized_mode,
        )

    return PushPrEligibilityDecision(
        eligible=False,
        reason=PUSH_PR_INELIGIBLE_STATUS,
        status_code=409,
        pr_mode=normalized_mode,
    )


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
    # Sanitize before persistence so /runs and /runs/{id} never surface raw
    # secret-like values (tokens, bearer credentials) from a push exception.
    safe_error = sanitize_for_log(error)
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
            "push_error": safe_error,
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
    # Do not silently default to main. validate_base_branch (called inside the
    # push flow) resolves a missing value to the safe default and rejects
    # forbidden base branches before any push or PR.
    base_branch = project.get("github_base_branch")
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


def _pr_title(feature_description: str | None, run_id: str) -> str:
    feature = (feature_description or "").strip()
    return feature if feature else f"Pipewright run {run_id[:8]}"


def _create_pr(repo, run_id: str, feature_description: str, branch_name: str, base_branch: str):
    return repo.create_pull(
        title=_pr_title(feature_description, run_id),
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


def _build_local_only_instructions(branch_name: str) -> list[str]:
    return [
        f"git checkout {branch_name}",
        f"git push origin {branch_name}",
        "Open a pull request on your Git host when you are ready.",
    ]


def _mark_local_only_complete(run_id: str, branch_name: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = :current_step,
                branch_name = COALESCE(branch_name, :branch_name),
                push_error = NULL
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "branch_name": branch_name,
            "status": RunStatus.COMPLETE,
            "current_step": "local_only_complete",
        })


def _complete_local_only(run_id: str, run: dict, project: dict, branch_name: str) -> dict:
    """
    local_only mode: no push, no PR, no GitHub token required.

    This is a safe, successful outcome — the approved changes live on a local
    branch and the operator finishes the PR by hand. It never marks the run as
    push_failed.
    """
    status = run.get("status")
    if status not in {
        RunStatus.FINAL_APPROVED,
        RunStatus.PUSH_FAILED,
        RunStatus.COMPLETE,
    }:
        raise RuntimeError(
            f"pr_orchestrator.py: run is not ready for push-pr. "
            f"run_id={run_id} | status={status}"
        )

    branch_name = run.get("branch_name") or branch_name
    # Read-only verification: confirm the approved branch exists locally. This
    # never pushes, checks out, or mutates git state.
    repo_path = project["repo_path"]
    local_git.ensure_git_repo(repo_path)
    if not local_git.branch_exists(branch_name, repo_path):
        raise RuntimeError(
            f"pr_orchestrator.py: expected local branch missing: {branch_name}"
        )

    _mark_local_only_complete(run_id, branch_name)
    return {
        "status": RunStatus.COMPLETE,
        "run_id": run_id,
        "pr_mode": PR_MODE_LOCAL_ONLY,
        "branch_name": branch_name,
        "pr_url": None,
        "pr_number": None,
        "remote_action": False,
        "message": (
            "Local-only mode: no branch was pushed and no pull request was "
            "created. The approved changes are committed on a local branch."
        ),
        "manual_instructions": _build_local_only_instructions(branch_name),
    }


def _verify_local_branch_for_cli(
    repo_path: str,
    branch_name: str,
    owner: str | None,
    repo_name: str | None,
) -> None:
    if owner and repo_name:
        # When owner/repo are configured, enforce the same origin-match safety
        # as the manual_token path.
        _verify_local_branch(repo_path, branch_name, owner, repo_name)
        return

    # owner/repo not configured: gh resolves the repo from the local remote
    # itself. Still verify branch presence, checkout, and a clean worktree
    # before any push happens.
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


def _push_and_create_pr_github_cli(
    run_id: str,
    run: dict,
    project: dict,
    branch_name: str,
) -> dict:
    """
    github_cli mode: push the approved branch with the safe git helper, then
    find-or-create the PR with gh. Never auto-merges, force-pushes, or deletes
    branches. gh is only touched after final approval.
    """
    repo_path = project["repo_path"]
    owner = project.get("github_owner")
    repo_name = project.get("github_repo")
    base_branch = project.get("github_base_branch")

    try:
        # Resolve a missing base branch to the safe default and reject
        # forbidden base branches (main/master/develop) before anything else.
        base_branch = validate_base_branch(base_branch)
        # #20B-2: the base branch must exist on the remote, or gh pr create would
        # fail later with an opaque GraphQL error. Check before any push / PR and
        # give a clear recovery message instead. Does not auto-push the base.
        ensure_remote_base_branch(repo_path, base_branch, "origin")
        # Fail safely with a clear message if gh is unusable, before any push.
        gh_pr.ensure_gh_ready()
        _mark_pushing(run_id, branch_name)
        _verify_local_branch_for_cli(repo_path, branch_name, owner, repo_name)
        _ensure_branch_has_commits_ahead(repo_path, branch_name, base_branch)
        if not local_git.branch_exists_remote(repo_path, branch_name):
            local_git.push_branch(branch_name, repo_path)
        _save_pushed(run_id, branch_name)

        existing = gh_pr.find_open_pr(repo_path, branch_name, base_branch)
        if existing is not None:
            pr = existing
        else:
            pr = gh_pr.create_pr(
                repo_path,
                branch_name,
                base_branch,
                _pr_title(run.get("feature_description"), run_id),
                build_pr_body(run_id),
            )
        return _save_pr_metadata(
            run_id,
            branch_name,
            pr["url"],
            int(pr["number"]),
        )
    except Exception as error:
        error_text = sanitize_for_log(str(error))
        _mark_push_failed(run_id, error_text, branch_name)
        raise RuntimeError(
            f"pr_orchestrator.py: push-pr failed. "
            f"run_id={run_id} | error={error_text}"
        )


def _push_and_create_pr_manual_token(
    run_id: str,
    run: dict,
    project: dict,
    branch_name: str,
) -> dict:
    """manual_token mode: the existing PyGithub push + PR flow, unchanged."""
    repo_path = project["repo_path"]
    token, owner, repo_name, base_branch = _require_project_github(project)

    try:
        # Resolve a missing base branch to the safe default and reject
        # forbidden base branches (main/master/develop) before any push or PR.
        base_branch = validate_base_branch(base_branch)
        # #20B-2: the base branch must exist on the remote, or create_pull would
        # fail later with an opaque GraphQL error. Check before any push / PR and
        # give a clear recovery message instead. Does not auto-push the base.
        ensure_remote_base_branch(repo_path, base_branch, "origin")
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
        # Sanitize before it is persisted (push_error) and before it is
        # re-raised into the HTTP response detail.
        error_text = sanitize_for_log(str(error))
        _mark_push_failed(run_id, error_text, branch_name)
        raise RuntimeError(
            f"pr_orchestrator.py: push-pr failed. "
            f"run_id={run_id} | error={error_text}"
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

    project = require_project(run.get("project_id"))
    pr_mode = normalize_pr_mode(project.get("pr_mode"))
    decision = evaluate_push_pr_eligibility(
        run_status=run.get("status"),
        pr_mode=pr_mode,
        has_pr_url=False,
    )

    if not decision.eligible:
        status = run.get("status")
        raise RuntimeError(
            f"pr_orchestrator.py: run is not ready for push-pr. "
            f"run_id={run_id} | status={status}"
        )

    if pr_mode == PR_MODE_LOCAL_ONLY:
        return _complete_local_only(run_id, run, project, branch_name)

    if pr_mode == PR_MODE_GITHUB_CLI:
        return _push_and_create_pr_github_cli(run_id, run, project, branch_name)
    if pr_mode == PR_MODE_MANUAL_TOKEN:
        return _push_and_create_pr_manual_token(run_id, run, project, branch_name)
    # normalize_pr_mode guarantees one of the three modes; this is defensive.
    raise RuntimeError(
        f"pr_orchestrator.py: unsupported pr_mode for push-pr: {pr_mode}"
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
