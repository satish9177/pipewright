"""
chunks.py
Routes for Phase 2B chunk planning, approval, execution, and manual resume.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from backend.core.statuses import ApprovalStatus, RunStatus
from backend.db.database import engine
from backend.models.handoff import (
    FEATURE_DESCRIPTION_MAX_LENGTH,
    REJECTION_REASON_MAX_LENGTH,
    _is_blank,
)
from backend.models.chunk import ChunkPlanResponse
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
    reject_chunk_plan,
)
from backend.models.chunk import TriageResult
from backend.pipeline.chunked_orchestrator import (
    approve_chunk_and_commit,
    execute_approved_chunks,
    reject_chunk_and_rollback,
    resume_chunked_pipeline,
)
from backend.pipeline.pr_orchestrator import push_and_create_pr
from backend.pipeline.implementation_guard import (
    DEFAULT_EXAMPLES,
    DEFAULT_MISSING_DETAILS,
    NEEDS_CLARIFICATION_MESSAGE,
    NON_ACTIONABLE_EXAMPLES,
    NON_ACTIONABLE_MESSAGE,
    NON_ACTIONABLE_MISSING_DETAILS,
    assess_implementation_specificity,
    is_non_actionable_request,
)
from backend.pipeline.intent import (
    IMPLEMENTATION,
    LLM_SPECIFICITY_MIN_CONFIDENCE,
    NEEDS_CLARIFICATION,
    PLAN_ONLY,
    REPORT_ONLY,
    SPECIFIC,
    classify_intent_details_async,
)
from backend.pipeline.plan_path_grounding import ground_triage_result_paths
from backend.pipeline.report_analyzer import (
    build_limited_report,
    run_report_analysis,
)
from backend.pipeline.risk_scanner import scan_triage_result
from backend.pipeline.run_locks import ProjectRepoLockError
from backend.pipeline.triage import run_triage
from backend.projects.project_store import get_project

logger = logging.getLogger(__name__)

router = APIRouter()
READ_ONLY_EXECUTION_MESSAGE = "This run is read-only and cannot execute code changes."


def _needs_clarification_response(
    message: str | None = None,
    missing_details: list[str] | None = None,
    examples: list[str] | None = None,
) -> JSONResponse:
    """
    Build the read-only needs_clarification envelope (HTTP 200). No run row is
    created and no triage/coder/patch/git/PR path is touched.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "needs_clarification",
            "intent": IMPLEMENTATION,
            "message": message or NEEDS_CLARIFICATION_MESSAGE,
            "missing_details": missing_details or DEFAULT_MISSING_DETAILS,
            "examples": examples or DEFAULT_EXAMPLES,
        },
    )


def _non_actionable_response() -> JSONResponse:
    """
    Build the needs_clarification envelope for a non-work input (greeting /
    noise). Intent is "unknown" because no report/plan/implementation decision
    has been made. No run row is created and intent classification is not even
    reached.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "needs_clarification",
            "intent": "unknown",
            "message": NON_ACTIONABLE_MESSAGE,
            "missing_details": NON_ACTIONABLE_MISSING_DETAILS,
            "examples": NON_ACTIONABLE_EXAMPLES,
        },
    )


class ChunkedRunRequest(BaseModel):
    project_id: str
    feature_description: str = Field(
        min_length=1,
        max_length=FEATURE_DESCRIPTION_MAX_LENGTH,
    )

    @field_validator("feature_description")
    @classmethod
    def feature_description_must_not_be_blank(cls, value: str) -> str:
        if _is_blank(value):
            raise ValueError("Field must not be blank")
        return value


class RejectChunkPlanRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectFinalApprovalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectChunkApprovalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


def _get_pending_final_gate(run_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT * FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND chunk_number = 0
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """), {"run_id": run_id}).fetchone()
    return dict(row._mapping) if row else None


def _update_run_final_status(run_id: str, status: str) -> None:
    with engine.connect() as conn:
        result = conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = :current_step
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "status": status,
            "current_step": status,
        })
        conn.commit()
    if result.rowcount == 0:
        raise ValueError(f"Run not found: {run_id}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_read_only_run(
    run_id: str,
    project_id: str,
    feature_description: str,
    intent: str,
    status: str,
    summary: str | None = None,
    chunk_plan: str | None = None,
    total_chunks: int = 0,
    report_json: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (
                id, project_id, feature_description, plain_english_summary,
                report_json, status, current_step, intent, chunk_plan_status,
                chunk_plan, total_chunks, current_chunk_number, created_at
            )
            VALUES
            (
                :id, :project_id, :feature_description, :summary,
                :report_json, :status, :current_step, :intent, 'none',
                :chunk_plan, :total_chunks, 0, :created_at
            )
        """), {
            "id": run_id,
            "project_id": project_id,
            "feature_description": feature_description,
            "summary": summary,
            "report_json": report_json,
            "status": status,
            "current_step": status,
            "intent": intent,
            "chunk_plan": chunk_plan,
            "total_chunks": total_chunks,
            "created_at": _utc_now(),
        })


def _load_run_intent(run_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT intent FROM pipeline_runs
            WHERE id = :run_id
        """), {"run_id": run_id}).fetchone()
    if row is None:
        return IMPLEMENTATION
    return row[0] or IMPLEMENTATION


def _ensure_mutating_run(run_id: str) -> None:
    if _load_run_intent(run_id) in {REPORT_ONLY, PLAN_ONLY}:
        raise RuntimeError(READ_ONLY_EXECUTION_MESSAGE)


def _decide_final_gate(
    run_id: str,
    gate_status: str,
    run_status: str,
    reason: str | None = None,
) -> dict:
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT * FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'final'
              AND chunk_number = 0
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """), {"run_id": run_id}).fetchone()
        if row is None:
            raise ValueError("Pending final approval gate not found")

        gate = dict(row._mapping)
        conn.execute(text("""
            UPDATE approval_gates
            SET status = :gate_status,
                rejection_reason = :reason,
                decided_at = :decided_at
            WHERE id = :gate_id
              AND status = 'pending'
        """), {
            "gate_status": gate_status,
            "reason": reason,
            "decided_at": _utc_now(),
            "gate_id": gate["id"],
        })
        result = conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :run_status,
                current_step = :run_status
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "run_status": run_status,
        })
        if result.rowcount == 0:
            raise ValueError(f"Run not found: {run_id}")
        return {"status": run_status, "run_id": run_id}


def _load_plan_run_for_handoff(run_id: str) -> dict:
    """
    Return the source plan run row for plan-to-implementation handoff, or
    raise HTTPException with the precise gating reason. The source run must
    still be a finished, read-only plan_only run with a usable plan.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT * FROM pipeline_runs WHERE id = :id"
        ), {"id": run_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")

    run = dict(row._mapping)
    if (run.get("intent") or "") != PLAN_ONLY:
        raise HTTPException(
            status_code=400,
            detail="Source run is not a plan-only run.",
        )
    if (run.get("status") or "") != RunStatus.PLAN_READY:
        raise HTTPException(
            status_code=400,
            detail="Source run is not in plan_ready state.",
        )
    if not run.get("project_id"):
        raise HTTPException(
            status_code=400,
            detail="Source plan run has no project_id.",
        )
    if not run.get("chunk_plan"):
        raise HTTPException(
            status_code=400,
            detail="Source plan run has no usable plan output.",
        )
    return run


def _find_existing_implementation_for_plan(run_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id FROM pipeline_runs
            WHERE source_plan_run_id = :source_plan_run_id
            ORDER BY created_at ASC
            LIMIT 1
        """), {"source_plan_run_id": run_id}).fetchone()
    return row[0] if row else None


@router.post(
    "/runs/{run_id}/start-implementation",
    response_model=ChunkPlanResponse,
)
async def start_implementation_from_plan_route(run_id: str):
    """
    Create (or return) an implementation run seeded from a plan_ready source
    run. The source run stays read-only; the new run enters the standard
    awaiting_chunk_plan_approval flow, so every existing safety gate still
    applies.
    """
    source_run = _load_plan_run_for_handoff(run_id)

    project = get_project(source_run["project_id"])
    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Source plan's project no longer exists.",
        )

    existing_id = _find_existing_implementation_for_plan(run_id)
    if existing_id is not None:
        try:
            return get_chunk_plan_status(existing_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error))

    try:
        seed_triage = TriageResult.model_validate_json(source_run["chunk_plan"])
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=f"Source plan output is not parseable: {error}",
        )

    new_run_id = str(uuid.uuid4())
    seed_triage = seed_triage.model_copy(update={"run_id": new_run_id})
    # Re-ground against the index (idempotent: the source plan_only run was
    # already grounded at creation) so the handoff never carries invented paths.
    seed_triage = ground_triage_result_paths(
        source_run["project_id"], seed_triage
    )
    seed_triage = scan_triage_result(seed_triage)

    try:
        return create_chunked_run(
            run_id=new_run_id,
            project_id=source_run["project_id"],
            feature_description=source_run["feature_description"],
            triage_result=seed_triage,
            intent=IMPLEMENTATION,
            source_plan_run_id=run_id,
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/runs/chunked", response_model=ChunkPlanResponse)
async def create_chunked_run_route(request: ChunkedRunRequest):
    project = get_project(request.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Pre-intent actionability guard: a greeting / noise-only message is not a
    # work request. Stop before intent classification so it never becomes a
    # plan_ready / report_ready / implementation run.
    if is_non_actionable_request(request.feature_description):
        logger.info(
            "[GUARD] Non-actionable request; needs clarification before "
            "intent classification. project_id=%s",
            request.project_id,
        )
        return _non_actionable_response()

    run_id = str(uuid.uuid4())
    decision = await classify_intent_details_async(request.feature_description)
    intent = decision.intent

    # Uncertain classification must not fall into the plan_only bucket and
    # fabricate a plan. If the request is not clearly report / plan /
    # implementation, ask for clarification instead of inventing scope.
    if decision.uncertain:
        logger.info(
            "[GUARD] Uncertain classification; needs clarification instead of "
            "a plan_only fallback. project_id=%s | source=%s | reason=%s",
            request.project_id,
            decision.source,
            decision.reason,
        )
        return _non_actionable_response()

    try:
        if intent == REPORT_ONLY:
            report_json: str | None = None
            try:
                analysis = await run_report_analysis(
                    run_id=run_id,
                    project_id=request.project_id,
                    feature_description=request.feature_description,
                )
                report = analysis.markdown_report
                # Structured source for ReportView. Absent on a limited/fallback
                # analysis; the run then renders plain_english_summary only.
                if analysis.report_result is not None:
                    report_json = analysis.report_result.model_dump_json()
            except Exception as analysis_error:
                # Defense in depth: the analyzer degrades internally, but if it
                # ever raises we still keep this run strictly read-only and
                # never fall through to triage/planner/coder.
                logger.warning(
                    "[REPORT] Analyzer raised; storing limited report. "
                    "run_id=%s | error=%s",
                    run_id,
                    analysis_error,
                )
                report = build_limited_report(
                    request.feature_description,
                    "analysis failed unexpectedly",
                )
            _create_read_only_run(
                run_id=run_id,
                project_id=request.project_id,
                feature_description=request.feature_description,
                intent=REPORT_ONLY,
                status=RunStatus.REPORT_READY,
                summary=report,
                report_json=report_json,
            )
            return ChunkPlanResponse(
                run_id=run_id,
                project_id=request.project_id,
                chunk_plan_status="none",
                total_chunks=0,
                current_chunk_number=0,
                triage=None,
                chunks=[],
            )

        if intent == IMPLEMENTATION:
            # Ambiguous-implementation guard (PR #9A): a vague implementation
            # request must not invent scope. We stop here, before triage /
            # chunk planning / run creation, and ask for details. Two signals,
            # combined conservatively (block if either says vague):
            #   1. deterministic guard on the raw text (no LLM)
            #   2. the LLM fallback's specificity verdict, but ONLY when the
            #      intent itself came from that same LLM call (no extra call)
            specificity = assess_implementation_specificity(
                request.feature_description
            )
            needs_clarification = not specificity.is_specific_enough
            block_reason = specificity.reason
            llm_message: str | None = None
            llm_missing: list[str] | None = None

            if decision.from_llm:
                if decision.specificity == NEEDS_CLARIFICATION:
                    needs_clarification = True
                    block_reason = "llm_specificity=needs_clarification"
                    llm_message = decision.clarification_message
                    llm_missing = decision.missing_details or None
                elif decision.specificity == SPECIFIC:
                    if (
                        decision.specificity_confidence or 0.0
                    ) < LLM_SPECIFICITY_MIN_CONFIDENCE:
                        needs_clarification = True
                        block_reason = "llm_specificity_confidence_low"
                else:
                    # LLM upgraded an ambiguous request to implementation but
                    # gave no usable specificity verdict — fail safe.
                    needs_clarification = True
                    block_reason = "llm_specificity_missing"

            if needs_clarification:
                logger.info(
                    "[GUARD] Blocked vague implementation request before "
                    "triage. run_id=%s | from_llm=%s | reason=%s",
                    run_id,
                    decision.from_llm,
                    block_reason,
                )
                return _needs_clarification_response(
                    message=llm_message,
                    missing_details=llm_missing or specificity.missing_details,
                    examples=specificity.examples,
                )

        triage_result = await run_triage(
            run_id=run_id,
            project_id=request.project_id,
            feature_description=request.feature_description,
        )
        # PR #9B: ground files_expected against the real repo index before the
        # risk scan and before persisting. Removes invented paths and hardens
        # affected chunks. Applies to both implementation and plan_only.
        triage_result = ground_triage_result_paths(
            request.project_id, triage_result
        )
        triage_result = scan_triage_result(triage_result)
        if intent == PLAN_ONLY:
            _create_read_only_run(
                run_id=run_id,
                project_id=request.project_id,
                feature_description=request.feature_description,
                intent=PLAN_ONLY,
                status=RunStatus.PLAN_READY,
                summary=triage_result.reasoning,
                chunk_plan=triage_result.model_dump_json(),
                total_chunks=triage_result.total_chunks,
            )
            return ChunkPlanResponse(
                run_id=run_id,
                project_id=request.project_id,
                chunk_plan_status="none",
                total_chunks=triage_result.total_chunks,
                current_chunk_number=0,
                triage=triage_result,
                chunks=[],
            )

        return create_chunked_run(
            run_id=run_id,
            project_id=request.project_id,
            feature_description=request.feature_description,
            triage_result=triage_result,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.get("/runs/{run_id}/chunks", response_model=ChunkPlanResponse)
def get_chunk_plan_route(run_id: str):
    try:
        return get_chunk_plan_status(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/runs/{run_id}/chunks/approve", response_model=ChunkPlanResponse)
def approve_chunk_plan_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return approve_chunk_plan(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/reject", response_model=ChunkPlanResponse)
def reject_chunk_plan_route(run_id: str, request: RejectChunkPlanRequest):
    try:
        _ensure_mutating_run(run_id)
        return reject_chunk_plan(run_id, request.reason)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/execute")
async def execute_chunks_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return await execute_approved_chunks(run_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/resume")
async def resume_chunks_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return await resume_chunked_pipeline(run_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/{chunk_number}/approve")
def approve_chunk_route(run_id: str, chunk_number: int):
    try:
        _ensure_mutating_run(run_id)
        return approve_chunk_and_commit(run_id, chunk_number)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/{chunk_number}/reject")
def reject_chunk_route(
    run_id: str,
    chunk_number: int,
    request: RejectChunkApprovalRequest,
):
    try:
        _ensure_mutating_run(run_id)
        return reject_chunk_and_rollback(run_id, chunk_number, request.reason)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/final-approval/approve")
def approve_final_approval_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return _decide_final_gate(
            run_id,
            ApprovalStatus.APPROVED,
            RunStatus.FINAL_APPROVED,
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/final-approval/reject")
def reject_final_approval_route(
    run_id: str,
    request: RejectFinalApprovalRequest,
):
    try:
        _ensure_mutating_run(run_id)
        return _decide_final_gate(
            run_id,
            ApprovalStatus.REJECTED,
            RunStatus.FINAL_REJECTED,
            request.reason or "Final approval rejected",
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/push-pr")
def push_pr_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        return push_and_create_pr(run_id)
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))
