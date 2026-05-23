"""
orchestrator.py
Main pipeline entry point.
Wires planner, coder, patch_applier, tester, and approval_gate.
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from backend.db.database import engine, init_db
from backend.models.handoff import (
    PlannerHandoff,
    CoderHandoff,
    PatchResult,
    TestResult,
    ApprovalRequest
)
from backend.pipeline.planner import run_planner
from backend.pipeline.coder import run_coder
from backend.pipeline.patch_applier import apply_patch
from backend.pipeline.tester import run_tests
from backend.pipeline.approval_gate import request_approval
from backend.projects.project_context import ProjectRuntimeConfig, active_project
from backend.projects.project_store import require_project


def _create_run(run_id: str, feature: str, project_id: str | None = None) -> None:
    try:
        init_db()
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_runs
                (id, project_id, feature_description, status, current_step, created_at)
                VALUES (:id, :project_id, :feature, 'running', 'created', :created_at)
            """), {
                "id": run_id,
                "project_id": project_id,
                "feature": feature,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            conn.commit()
    except Exception as error:
        raise RuntimeError(
            f"orchestrator.py: failed to create run. "
            f"run_id={run_id} | error={error}"
        )


def _update_run_status(
    run_id: str,
    status: str,
    current_step: str
) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                UPDATE pipeline_runs
                SET status = :status,
                    current_step = :current_step
                WHERE id = :id
            """), {
                "id": run_id,
                "status": status,
                "current_step": current_step,
            })
            conn.commit()
    except Exception as error:
        raise RuntimeError(
            f"orchestrator.py: failed to update run status. "
            f"run_id={run_id} | status={status} | "
            f"current_step={current_step} | error={error}"
        )


def _fail(run_id: str, stage: str, error) -> dict:
    print(f"[PIPELINE] Failed at stage={stage} | error={error}")
    return {
        "run_id": run_id,
        "status": "failed",
        "failed_stage": stage,
        "error": str(error)
    }


def _build_summary(
    plan: PlannerHandoff,
    coder_output: CoderHandoff,
    test_result: TestResult
) -> str:
    files = [f.path for f in coder_output.files_changed]
    return (
        f"Goal: {plan.goal}\n"
        f"Files changed: {', '.join(files)}\n"
        f"Tests: {test_result.passed_tests} passed, "
        f"{test_result.failed_tests} failed\n"
        f"Summary: {coder_output.summary}"
    )


def _surface_memory_suggestions(
    plan: PlannerHandoff,
    coder_output: CoderHandoff
) -> None:
    suggestions = (
        plan.suggested_memory_entries +
        coder_output.suggested_memory_entries
    )
    if not suggestions:
        return
    print("[PIPELINE] Memory suggestions from this run:")
    for i, suggestion in enumerate(suggestions, 1):
        print(f"  {i}. {suggestion}")
    print("[PIPELINE] Review these and add to memory manually if relevant.")


def _load_project_runtime(project_id: str | None) -> ProjectRuntimeConfig | None:
    if project_id is None:
        return None

    try:
        project = require_project(project_id)
        return ProjectRuntimeConfig(
            project_id=project["id"],
            repo_path=project["repo_path"],
            test_command=project["test_command"],
        )
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"orchestrator.py: failed to load project. "
            f"project_id={project_id} | error={error}"
        )


async def _run_pipeline_steps(
    feature_description: str,
    run_id: str
) -> dict:
    print(f"[PIPELINE] Started | run_id={run_id}")

    try:
        _update_run_status(run_id, "running", "plan")
        plan = await run_planner(feature_description, run_id)
        print("[PIPELINE] Stage 1 complete: plan")
    except Exception as error:
        _update_run_status(run_id, "failed", "plan")
        return _fail(run_id, "plan", error)

    try:
        _update_run_status(run_id, "running", "code")
        coder_output = await run_coder(plan, run_id)
        print("[PIPELINE] Stage 2 complete: code")
    except Exception as error:
        _update_run_status(run_id, "failed", "code")
        return _fail(run_id, "code", error)

    try:
        _update_run_status(run_id, "running", "patch")
        patch_result = apply_patch(coder_output, run_id)
        print("[PIPELINE] Stage 3 complete: patch")
    except Exception as error:
        _update_run_status(run_id, "failed", "patch")
        return _fail(run_id, "patch", error)

    try:
        _update_run_status(run_id, "running", "test")
        test_result = run_tests(patch_result, run_id)
        print(f"[PIPELINE] Stage 4 complete: test | passed={test_result.passed}")

        if not test_result.passed:
            _update_run_status(run_id, "failed", "test")
            return _fail(run_id, "test", "Tests failed. Rollback triggered.")
    except Exception as error:
        _update_run_status(run_id, "failed", "test")
        return _fail(run_id, "test", error)

    try:
        _update_run_status(run_id, "paused", "approval")
        ai_summary = _build_summary(plan, coder_output, test_result)
        approval = await request_approval(
            test_result=test_result,
            run_id=run_id,
            diff=patch_result.diff,
            ai_summary=ai_summary
        )
        print(
            f"[PIPELINE] Stage 5 complete: approval | "
            f"approved={approval.approved}"
        )
        if not approval.approved:
            _update_run_status(run_id, "rejected", "approval")
            print(f"[PIPELINE] Rejected by human. Rolling back | run_id={run_id}")
            try:
                from backend.pipeline.patch_applier import rollback_patch
                rollback_patch(run_id)
                print(f"[PIPELINE] Rollback complete | run_id={run_id}")
            except Exception as rb_error:
                print(f"[PIPELINE] Rollback failed | error={rb_error}")
            return {
                "run_id": run_id,
                "status": "rejected",
                "reason": "Human rejected the changes. Files rolled back."
            }

    except Exception as error:
        _update_run_status(run_id, "failed", "approval")
        return _fail(run_id, "approval", error)

    _update_run_status(run_id, "complete", "done")
    _surface_memory_suggestions(plan, coder_output)

    print("[PIPELINE] Complete | run_id=" + run_id)
    return {
        "run_id": run_id,
        "status": "complete",
        "feature": feature_description,
        "tests_passed": test_result.passed,
        "files_changed": [f.path for f in coder_output.files_changed],
        "approved": True
    }


async def _run_pipeline_with_id(
    feature_description: str,
    run_id: str,
    project_id: str | None = None
) -> dict:
    project_runtime = _load_project_runtime(project_id)
    _create_run(run_id, feature_description, project_id)

    if project_runtime is None:
        return await _run_pipeline_steps(feature_description, run_id)

    with active_project(project_runtime):
        print(
            f"[PIPELINE] Project selected | "
            f"project_id={project_runtime.project_id}"
        )
        return await _run_pipeline_steps(feature_description, run_id)


async def run_pipeline(
    feature_description: str,
    project_id: str | None = None
) -> dict:
    """
    Main pipeline entry point.
    Runs all stages in order.
    Returns summary dict with run_id,
    status, and what happened.
    """
    run_id = str(uuid.uuid4())
    return await _run_pipeline_with_id(feature_description, run_id, project_id)
