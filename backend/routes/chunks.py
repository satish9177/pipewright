"""
chunks.py
Routes for Phase 2B chunk planning, approval, execution, and manual resume.
"""

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
    get_chunk,
    get_chunk_plan_status,
    get_chunk_test_run_verdict,
    reject_chunk_plan,
)
from backend.pipeline.test_validation_ack_store import (
    ACK_REQUIRED_VERDICTS,
    TestValidationAckConflictError,
    acknowledgement_state,
    chunks_requiring_acknowledgement,
    compute_chunk_diff_hash,
    create_acknowledgement,
    evaluate_final_approval_ack_eligibility,
)
from backend.models.chunk import TriageResult
from backend.pipeline.chunked_orchestrator import (
    approve_and_retry_scope_expansion,
    approve_chunk_and_commit,
    execute_approved_chunks,
    reject_chunk_and_rollback,
    retry_failed_chunk,
    resume_chunked_pipeline,
    settle_run_after_scope_expansion_reject,
)
from backend.pipeline.scope_expansion import ScopeExpansionValidationError
from backend.pipeline.scope_expansion_store import (
    ScopeExpansionConflictError,
    reject_scope_expansion_request,
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
from backend.pipeline.file_alias_grounding import (
    EditTargetOutcome,
    EditTargetResolution,
    resolve_explicit_edit_target,
)
from backend.pipeline.file_candidate_ranking import (
    Recommendation,
    rank_ambiguous_file_candidates,
)
from backend.pipeline.clarification_context import (
    ClarificationDecodeStatus,
    create_clarification_context,
    decode_clarification_context,
    encode_clarification_context,
)
from backend.pipeline.clarification_selection import (
    SelectionStatus,
    parse_clarification_selection,
)
from backend.pipeline.plan_path_grounding import (
    ground_triage_result_paths,
    get_indexed_paths_and_dirs,
)
from backend.pipeline.file_scope_intent import (
    extract_user_file_constraints,
    reconcile_file_scope,
)
from backend.pipeline.report_analyzer import (
    build_limited_report,
    run_report_analysis,
)
from backend.pipeline.risk_scanner import scan_triage_result
from backend.pipeline.run_locks import (
    ProjectRepoLockError,
    project_repo_lock_sync,
)
from backend.pipeline.triage import run_triage
from backend.projects.project_store import get_project
from backend.repo.repo_indexer import build_repo_index, is_supported_file
from backend.utils.path_safety import is_forbidden_path

logger = logging.getLogger(__name__)

router = APIRouter()
READ_ONLY_EXECUTION_MESSAGE = "This run is read-only and cannot execute code changes."


def _needs_clarification_response(
    message: str | None = None,
    missing_details: list[str] | None = None,
    examples: list[str] | None = None,
    candidates: list[str] | None = None,
    recommended_path: str | None = None,
    recommendation_strength: str | None = None,
    clarification_id: str | None = None,
) -> JSONResponse:
    """
    Build the read-only needs_clarification envelope (HTTP 200). No run row is
    created and no triage/coder/patch/git/PR path is touched.
    """
    content = {
        "status": "needs_clarification",
        "intent": IMPLEMENTATION,
        "message": message or NEEDS_CLARIFICATION_MESSAGE,
        "missing_details": missing_details or DEFAULT_MISSING_DETAILS,
        "examples": examples or DEFAULT_EXAMPLES,
    }
    if candidates is not None:
        content["candidates"] = candidates
        content["recommended_path"] = recommended_path
        content["recommendation_strength"] = (
            recommendation_strength or Recommendation.NONE.value
        )
    # Additive: present only on the ambiguous-file clarification so the follow-up
    # selection endpoint (#17M) can map a reply within this exact candidate set.
    if clarification_id is not None:
        content["clarification_id"] = clarification_id
    return JSONResponse(status_code=200, content=content)


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


# PR #17C: explicit-edit-target grounding. Canonical filename used in
# clarification copy when an aliased target is missing. Explicit paths map to
# themselves (handled by .get default).
_CANONICAL_CREATE_NAME = {
    "readme": "README.md",
    "package.json": "package.json",
    "docker compose": "docker-compose.yml",
    "requirements": "requirements.txt",
    "pyproject": "pyproject.toml",
}


def _canonical_name(label: str) -> str:
    return _CANONICAL_CREATE_NAME.get(label, label)


def _mentions_create(feature_description: str) -> bool:
    """True when the request explicitly asks to create a file."""
    words = (feature_description or "").lower().replace("-", " ").split()
    return "create" in words


def _unsupported_extensionless_create_target(
    feature_description: str,
) -> str | None:
    """Catch explicit create requests for known extensionless files we defer."""
    words = {
        word.strip("\"'`(),;:!?[]{}<>").lower()
        for word in (feature_description or "").replace("-", " ").split()
    }
    if "create" in words and "license" in words:
        return "LICENSE"
    return None


def _target_not_found_response(label: str, wants_create: bool) -> JSONResponse:
    """
    Specific clarification when the user named a file target that is not in the
    repo index. We never invent or auto-create files (PR #17C).
    """
    canonical = _canonical_name(label)
    if wants_create:
        message = (
            f"I understood you want to create {canonical}, but Pipewright does "
            "not create new files automatically yet. Add it to the repository "
            "(and re-index), then ask again — or point me at an existing file "
            "to edit."
        )
        missing_details = [
            f"create {canonical} in the repository first, then retry",
            "or name an existing file to edit",
        ]
    else:
        message = (
            f"I understood you want to edit the {label} file, but no matching "
            f"file was found in this project. Do you want to create {canonical}? "
            "Pipewright will not create it automatically."
        )
        missing_details = [
            "an existing target file in this project",
            f"or create {canonical} yourself first",
        ]
    return _needs_clarification_response(
        message=message,
        missing_details=missing_details,
    )


def _target_ambiguous_response(
    label: str,
    candidates: tuple[str, ...],
    feature_description: str,
    project_id: str,
    *,
    note: str | None = None,
) -> JSONResponse:
    """Specific clarification when several indexed files match the target."""
    ranked = rank_ambiguous_file_candidates(feature_description, candidates)
    ordered = list(ranked.ordered)
    listed = ", ".join(ordered)
    numbered = "\n".join(
        f"{index}. {path}" for index, path in enumerate(ordered, start=1)
    )
    label_text = "README files" if label == "readme" else f'files matching "{label}"'

    if ranked.recommendation is Recommendation.NONE or ranked.recommended is None:
        message = (
            f"I found multiple {label_text}. Choose one exact path:\n"
            f"{numbered}"
        )
    else:
        reason = _ambiguous_recommendation_reason(
            ranked.reason_tokens,
            ranked.recommended,
        )
        message = (
            f"I found multiple {label_text}. I think you mean "
            f"{ranked.recommended}{reason}. Confirm, or choose one:\n"
            f"{numbered}"
        )
    if note:
        message = f"{note} {message}"

    # Mint a signed, self-expiring context so the user's follow-up reply
    # ("1" / "yes 1" / "use README.md") can be mapped to one of THESE candidates
    # on the selection endpoint. Stateless: no run row, no DB write. The original
    # intent is preserved so the selection can rebuild the request. Failure to
    # encode must never break the safe clarification, so it degrades to omitting
    # clarification_id (older behavior).
    clarification_id: str | None = None
    try:
        context = create_clarification_context(
            project_id=project_id,
            original_feature_description=feature_description,
            alias=label,
            candidates=ordered,
            recommended_path=ranked.recommended,
            recommendation_strength=ranked.recommendation.value,
        )
        clarification_id = encode_clarification_context(context)
    except Exception as error:  # pragma: no cover - defensive only
        logger.warning(
            "[CLARIFY] Failed to mint clarification_id; returning clarification "
            "without selection handoff. error=%s",
            error,
        )

    return _needs_clarification_response(
        message=message,
        missing_details=[f"which exact path to edit: {listed}"],
        candidates=ordered,
        recommended_path=ranked.recommended,
        recommendation_strength=ranked.recommendation.value,
        clarification_id=clarification_id,
    )


def _ambiguous_recommendation_reason(
    reason_tokens: tuple[str, ...],
    recommended_path: str | None = None,
) -> str:
    """Render ranker reason tokens as concise user-facing copy."""
    matched: list[str] = []
    excluded: list[str] = []
    phrases: list[str] = []

    for token in reason_tokens:
        if token == "exact-path":
            phrases.append("you named that exact path")
        elif token == "root-level":
            phrases.append("it is root-level")
        elif token.startswith("matched:"):
            matched.append(token.split(":", 1)[1])
        elif token.startswith("excluded:"):
            excluded.append(token.split(":", 1)[1])

    if matched and recommended_path:
        path_order = {
            token: index
            for index, token in enumerate(
                part.lower()
                for part in recommended_path.replace("\\", "/").split("/")
            )
        }
        matched.sort(key=lambda token: path_order.get(token, len(path_order)))
    if matched:
        phrases.append(f"it matches {'/'.join(matched)}")
    if excluded:
        phrases.append(f"your request excludes {'/'.join(excluded)}")
    if not phrases:
        return ""
    return " because " + " and ".join(phrases)


def _target_forbidden_response(label: str) -> JSONResponse:
    """Safe refusal when the named target is a protected/secret path."""
    return _needs_clarification_response(
        message=(
            f"I understood the target ({label}), but that file is protected and "
            "cannot be modified automatically."
        ),
        missing_details=["a different, non-protected target file"],
    )


def _target_excluded_response(path: str) -> JSONResponse:
    """
    Clarification when a named file exists on disk but is not in the repo index
    because its type is unsupported or it is excluded by the indexer (PR #19C).

    Distinct from the create-file clarification: the file is present, so telling
    the user to "create it first" would be wrong.
    """
    return _needs_clarification_response(
        message=(
            f"{path} exists on disk, but it is not included in Pipewright's "
            "repo index because its file type is unsupported or excluded. "
            "Pipewright can only edit supported, indexed text/code files."
        ),
        missing_details=["a supported, indexed file to edit"],
    )


@dataclass
class _StaleReindexRecovery:
    """
    Result of a PR #19C stale explicit-path recovery attempt.

    - ``response`` set -> return it immediately (a clarification/refusal).
    - ``resolution`` set -> continue the normal flow with this re-resolved
      target (only GROUNDED / CREATE_TARGET are handed back this way).
    - both None -> no recovery happened; caller keeps the original NOT_FOUND
      (create-file) clarification.
    """

    response: JSONResponse | None = None
    resolution: EditTargetResolution | None = None


def _safe_repo_relative_path(token: str | None) -> str | None:
    """
    Normalize a target token to a safe repo-relative path, or None.

    Rejects empty, absolute, and parent-traversal (``..``) tokens. Pure: never
    touches the filesystem.
    """
    if not token or not isinstance(token, str):
        return None
    if os.path.isabs(token):
        return None
    norm = token.replace("\\", "/").strip()
    if not norm or norm.startswith("/"):
        return None
    parts = [part for part in norm.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _is_forbidden_repo_path(path: str) -> bool:
    """
    True when a repo-relative path is a protected target: a read-level
    secret/.env match OR any ``.git`` path component. Fails safe (treats an
    unexpected error as forbidden).
    """
    try:
        if is_forbidden_path(path):
            return True
    except Exception:
        return True
    return ".git" in [part for part in path.split("/") if part]


def _attempt_stale_explicit_target_reindex(
    project_id: str,
    target_repo_path: str | None,
    feature_description: str,
    alias: str | None,
) -> _StaleReindexRecovery:
    """
    PR #19C: single-shot stale explicit-path recovery.

    Only called when ``resolve_explicit_edit_target`` returned NOT_FOUND for an
    explicit path/alias. If the named target actually exists on disk under the
    project repo, re-index ONCE and re-resolve. Hard-capped at a single re-index
    (no loops, no recursion). Never auto-creates a file, never relaxes
    forbidden/secret paths, and never walks the filesystem (a single
    ``Path.is_file()`` probe only).

    Lock-aware: re-index runs under the same project repo lock used by the #19B
    endpoint, so it cannot race an active run; a held lock surfaces as HTTP 409.
    """
    if not target_repo_path or not alias:
        return _StaleReindexRecovery()

    probe = _safe_repo_relative_path(_canonical_name(alias))
    if probe is None:
        return _StaleReindexRecovery()

    # Never re-index toward a forbidden/secret target. The resolver already
    # returns FORBIDDEN for these, but guard defensively and fail safe.
    probe_parts = [part for part in probe.split("/") if part]
    try:
        if is_forbidden_path(probe) or ".git" in probe_parts:
            return _StaleReindexRecovery(
                response=_target_forbidden_response(alias)
            )
    except Exception:
        return _StaleReindexRecovery(
            response=_target_forbidden_response(alias)
        )

    target_on_disk = Path(target_repo_path) / probe
    try:
        exists_on_disk = target_on_disk.is_file()
    except OSError:
        exists_on_disk = False

    if not exists_on_disk:
        # Genuinely missing on disk: keep the existing create/not-found path.
        return _StaleReindexRecovery()

    # Present on disk, but the indexer would never include this file type, so a
    # re-index cannot help. Explain rather than say "create it".
    if not is_supported_file(target_on_disk):
        return _StaleReindexRecovery(response=_target_excluded_response(probe))

    # Present on disk and indexable: the index is simply stale. Re-index once
    # (lock-aware) and re-resolve.
    try:
        with project_repo_lock_sync(project_id):
            build_repo_index(project_id, target_repo_path)
    except ProjectRepoLockError as error:
        # A run is actively using this project's repo; do not race it.
        raise HTTPException(status_code=409, detail=str(error))
    except Exception as error:
        # A re-index failure must not 500 the request; keep the safe
        # clarification and log the sanitized detail server-side.
        logger.warning(
            "[GROUND-TARGET] Stale re-index failed; keeping clarification. "
            "project_id=%s | error=%s",
            project_id,
            error,
        )
        return _StaleReindexRecovery()

    re_resolved = resolve_explicit_edit_target(
        project_id,
        feature_description,
        allow_create_target=True,
    )
    outcome = re_resolved.outcome

    if outcome in (
        EditTargetOutcome.GROUNDED,
        EditTargetOutcome.CREATE_TARGET,
    ):
        return _StaleReindexRecovery(resolution=re_resolved)
    if outcome is EditTargetOutcome.AMBIGUOUS:
        return _StaleReindexRecovery(
            response=_target_ambiguous_response(
                re_resolved.alias or alias,
                re_resolved.candidates,
                feature_description,
                project_id,
            )
        )
    if outcome is EditTargetOutcome.FORBIDDEN:
        return _StaleReindexRecovery(
            response=_target_forbidden_response(re_resolved.alias or alias)
        )

    # Still NOT_FOUND / NO_TARGET after a fresh scan even though the file is on
    # disk. Two safe sub-cases (never silently rewrite, never auto-create):
    #   1. a case-insensitive index match exists -> offer the real path as a
    #      candidate (e.g. user typed readme.md but README.md is indexed);
    #   2. otherwise the file is on disk but excluded from the index -> explain.
    index = get_indexed_paths_and_dirs(project_id)
    ci_matches = sorted(
        path for path in index.paths if path.lower() == probe.lower()
    )
    if ci_matches:
        return _StaleReindexRecovery(
            response=_target_ambiguous_response(
                alias,
                tuple(ci_matches),
                feature_description,
                project_id,
            )
        )
    return _StaleReindexRecovery(response=_target_excluded_response(probe))


def _pin_single_chunk_files_expected(
    triage_result: TriageResult,
    path: str,
    *,
    create_target: bool = False,
) -> TriageResult:
    """
    Pin a single grounded edit target onto a one-chunk plan (PR #17C).

    Deterministically narrows ``files_expected`` to ``[path]`` for a simple
    explicit file edit/create. Conservative on multi-chunk plans: a single named
    target must not be forced onto several chunks, so those are left untouched
    and flow through normal grounding. The pinned path is resolver-sanctioned and
    still passes through ground_triage_result_paths, scan_triage_result, and
    scope_guard unchanged.
    """
    if triage_result.total_chunks != 1 or len(triage_result.chunks) != 1:
        logger.info(
            "[GROUND-TARGET] Skipping files_expected pin on multi-chunk plan "
            "(total_chunks=%s); leaving to normal grounding. path=%s",
            triage_result.total_chunks,
            path,
        )
        return triage_result

    chunk = triage_result.chunks[0]
    description = chunk.description
    if create_target:
        create_note = f"Create {path} and add the requested content."
        if create_note not in description:
            description = f"{description.rstrip()} {create_note}".strip()

    if chunk.files_expected == [path] and description == chunk.description:
        return triage_result
    pinned = chunk.model_copy(update={
        "files_expected": [path],
        "description": description,
    })
    return triage_result.model_copy(update={"chunks": [pinned]})


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


class ClarificationSelectionRequest(BaseModel):
    project_id: str
    selection: str = Field(min_length=1, max_length=FEATURE_DESCRIPTION_MAX_LENGTH)


class RejectChunkPlanRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectFinalApprovalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectChunkApprovalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RetryChunkRequest(BaseModel):
    failure_report_id: str = Field(min_length=1)

    @field_validator("failure_report_id")
    @classmethod
    def failure_report_id_must_not_be_blank(cls, value: str) -> str:
        if _is_blank(value):
            raise ValueError("Field must not be blank")
        return value.strip()


class RejectMemoryConflictRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class AcknowledgeTestValidationRequest(BaseModel):
    # Optional human reason for committing despite weak/no-test validation
    # ("small change, manually verified"). Stored as audited context only; it
    # never relaxes any check.
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class RejectScopeExpansionRequest(BaseModel):
    # Optional reason, matching the other reject endpoints in this module. A blank
    # reason is stored as a safe default by the store layer.
    reason: str | None = Field(default=None, max_length=REJECTION_REASON_MAX_LENGTH)


class ApproveScopeExpansionRequest(BaseModel):
    # The human-approved expanded allowlist (a subset of the request's untrusted
    # requested_files). Left unconstrained here so the store/pure-validation layer
    # is the single source of truth for emptiness/safety (an empty or unsafe set
    # yields a 422 from validate_approved_files, keeping the request pending).
    approved_files: list[str] = Field(default_factory=list)
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


def _decide_memory_conflict_gate(
    run_id: str,
    gate_status: str,
    run_status: str,
    reason: str | None = None,
) -> dict:
    """
    Decide the pending DB memory-conflict gate (#16D-4) and set the run status.

    Approve flips the gate to ``approved`` and the run back to an executable state;
    continuation is the existing explicit execute/resume call, which honors the
    approved override. Reject fails the run before any branch/patch/commit.
    """
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT * FROM approval_gates
            WHERE run_id = :run_id
              AND approval_type = 'memory_conflict'
              AND chunk_number = 0
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """), {"run_id": run_id}).fetchone()
        if row is None:
            raise ValueError("Pending memory conflict gate not found")

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
    return await _create_chunked_run_core(
        request.project_id,
        request.feature_description,
    )


async def _create_chunked_run_core(
    project_id: str,
    feature_description: str,
    *,
    selected_path: str | None = None,
):
    """
    Shared chunked-run creation core (PR #17M refactor).

    Behavior-preserving extraction of the original ``/runs/chunked`` body so the
    clarification-selection endpoint can re-enter the *exact same* path — pre-
    intent guard, intent classification, explicit-target resolver/grounding, risk
    scan, scope pinning, and read-only report/plan handling. No guard is skipped:
    a rebuilt selection request is re-resolved against the live index here, so the
    selected path is never trusted as edit authority.

    ``selected_path`` (#20B-1) is set ONLY by the clarification-selection
    endpoint, never by the public ``/runs/chunked`` route. It carries the exact
    path the user picked from a previously-shown, signed candidate set. When
    present it is authoritative over any (possibly wrong-case) alias still in the
    rebuilt request text — but it is still re-validated against the live index
    here, so it is never trusted as edit authority.
    """
    project = get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    # PR #19C: repo path for the stale explicit-target re-index recovery below.
    target_repo_path = project.get("repo_path")

    # Pre-intent actionability guard: a greeting / noise-only message is not a
    # work request. Stop before intent classification so it never becomes a
    # plan_ready / report_ready / implementation run.
    if is_non_actionable_request(feature_description):
        logger.info(
            "[GUARD] Non-actionable request; needs clarification before "
            "intent classification. project_id=%s",
            project_id,
        )
        return _non_actionable_response()

    run_id = str(uuid.uuid4())
    decision = await classify_intent_details_async(feature_description)
    intent = decision.intent

    # Uncertain classification must not fall into the plan_only bucket and
    # fabricate a plan. If the request is not clearly report / plan /
    # implementation, ask for clarification instead of inventing scope.
    if decision.uncertain:
        logger.info(
            "[GUARD] Uncertain classification; needs clarification instead of "
            "a plan_only fallback. project_id=%s | source=%s | reason=%s",
            project_id,
            decision.source,
            decision.reason,
        )
        return _non_actionable_response()

    # PR #17C: when a simple explicit file edit grounds to a single indexed
    # file, pin files_expected to it after triage. Set only in the
    # implementation branch below; None means "no pin / unchanged flow".
    pinned_path: str | None = None
    create_target_path: str | None = None

    try:
        if intent == REPORT_ONLY:
            report_json: str | None = None
            try:
                analysis = await run_report_analysis(
                    run_id=run_id,
                    project_id=project_id,
                    feature_description=feature_description,
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
                    feature_description,
                    "analysis failed unexpectedly",
                )
            _create_read_only_run(
                run_id=run_id,
                project_id=project_id,
                feature_description=feature_description,
                intent=REPORT_ONLY,
                status=RunStatus.REPORT_READY,
                summary=report,
                report_json=report_json,
            )
            return ChunkPlanResponse(
                run_id=run_id,
                project_id=project_id,
                chunk_plan_status="none",
                total_chunks=0,
                current_chunk_number=0,
                triage=None,
                chunks=[],
            )

        if intent == IMPLEMENTATION:
            # #20B-1: clarification-selection handoff. When the user picked an
            # exact path from a previously-shown, signed candidate set, that
            # selected path is AUTHORITATIVE over any (possibly wrong-case) alias
            # still present in the rebuilt request text. Without this, the alias
            # resolver below would re-derive the original lowercase token first
            # (e.g. "manual.md" before "MANUAL.md") and loop the same
            # clarification. We still (1) re-validate the path against the LIVE
            # index and (2) never relax forbidden paths, so the selection is
            # never trusted as edit authority. The selection endpoint has already
            # proven the path is one of the signed candidates and that "1"/index
            # forms only resolve within that set (no global numeric selection).
            if selected_path is not None:
                safe_selected = _safe_repo_relative_path(selected_path)
                if safe_selected is not None and _is_forbidden_repo_path(
                    safe_selected
                ):
                    logger.info(
                        "[CLARIFY-SELECT] Selected path is protected; refusing. "
                        "run_id=%s | path=%s",
                        run_id,
                        selected_path,
                    )
                    return _target_forbidden_response(selected_path)
                if (
                    safe_selected is None
                    or safe_selected
                    not in get_indexed_paths_and_dirs(project_id).paths
                ):
                    logger.info(
                        "[CLARIFY-SELECT] Selected path no longer in the live "
                        "index; safe clarification, no run. run_id=%s | path=%s",
                        run_id,
                        selected_path,
                    )
                    return _target_not_found_response(selected_path, False)
                pinned_path = safe_selected
                logger.info(
                    "[CLARIFY-SELECT] Pinned user-selected path; bypassing alias "
                    "resolver to avoid the case-mismatch loop. run_id=%s | "
                    "path=%s",
                    run_id,
                    pinned_path,
                )

            # PR #17C: explicit-edit-target grounding runs FIRST. When the user
            # named a literal file / explicit path that resolves against the repo
            # index, that grounded path is a concrete anchor — the request is
            # specific even if the generic heuristic below would call it vague
            # (e.g. "add hello in the readme", where "readme" fuzzily collapses
            # into "rename"). A grounded target therefore bypasses the vague
            # guard; a missing/ambiguous/forbidden target yields a SPECIFIC
            # clarification instead of the generic one.
            #
            # Only consult the resolver when the project already has a non-empty
            # repo index. The index is built lazily by run_triage below, so an
            # empty index here means the project simply has not been indexed yet
            # (not that the target is missing); in that case we skip the resolver
            # and fall through to the existing guard rather than emit a false
            # clarification. Skipped entirely when a clarification selection has
            # already pinned the target above.
            if selected_path is None and not get_indexed_paths_and_dirs(
                project_id
            ).is_empty:
                resolution = resolve_explicit_edit_target(
                    project_id,
                    feature_description,
                    allow_create_target=True,
                )
                if resolution.outcome is EditTargetOutcome.NOT_FOUND:
                    # PR #19C: the index may simply be stale. If the named
                    # explicit target actually exists on disk, re-index ONCE and
                    # re-resolve before falling back to the create/not-found
                    # clarification. Single attempt; never auto-creates; never
                    # relaxes forbidden paths.
                    recovery = _attempt_stale_explicit_target_reindex(
                        project_id,
                        target_repo_path,
                        feature_description,
                        resolution.alias,
                    )
                    if recovery.response is not None:
                        return recovery.response
                    if recovery.resolution is not None:
                        logger.info(
                            "[GROUND-TARGET] Stale index; re-indexed and "
                            "re-resolved named target. run_id=%s | label=%s "
                            "| outcome=%s",
                            run_id,
                            resolution.alias,
                            recovery.resolution.outcome.value,
                        )
                        resolution = recovery.resolution
                    else:
                        logger.info(
                            "[GROUND-TARGET] Named target not indexed; "
                            "clarifying. run_id=%s | label=%s",
                            run_id,
                            resolution.alias,
                        )
                        return _target_not_found_response(
                            resolution.alias,
                            _mentions_create(feature_description),
                        )
                if resolution.outcome is EditTargetOutcome.AMBIGUOUS:
                    logger.info(
                        "[GROUND-TARGET] Named target ambiguous; clarifying. "
                        "run_id=%s | label=%s | candidates=%s",
                        run_id,
                        resolution.alias,
                        list(resolution.candidates),
                    )
                    return _target_ambiguous_response(
                        resolution.alias,
                        resolution.candidates,
                        feature_description,
                        project_id,
                    )
                if resolution.outcome is EditTargetOutcome.FORBIDDEN:
                    logger.info(
                        "[GROUND-TARGET] Named target is protected; refusing. "
                        "run_id=%s | label=%s",
                        run_id,
                        resolution.alias,
                    )
                    return _target_forbidden_response(resolution.alias)
                if resolution.outcome is EditTargetOutcome.GROUNDED:
                    pinned_path = resolution.path
                    logger.info(
                        "[GROUND-TARGET] Grounded explicit target; pinning "
                        "files_expected and bypassing vague guard. "
                        "run_id=%s | path=%s",
                        run_id,
                        pinned_path,
                    )
                if resolution.outcome is EditTargetOutcome.CREATE_TARGET:
                    pinned_path = resolution.path
                    create_target_path = resolution.path
                    logger.info(
                        "[GROUND-TARGET] Sanctioned create target; pinning "
                        "files_expected and bypassing vague guard. "
                        "run_id=%s | path=%s",
                        run_id,
                        create_target_path,
                    )
                unsupported_create_label = _unsupported_extensionless_create_target(
                    feature_description
                )
                if (
                    resolution.outcome is EditTargetOutcome.NO_TARGET
                    and unsupported_create_label is not None
                ):
                    logger.info(
                        "[GROUND-TARGET] Unsupported extensionless create "
                        "target; clarifying. run_id=%s | label=%s",
                        run_id,
                        unsupported_create_label,
                    )
                    return _target_not_found_response(
                        unsupported_create_label,
                        True,
                    )
                # NO_TARGET → fall through to the existing specificity guard.

            # Ambiguous-implementation guard (PR #9A): a vague implementation
            # request must not invent scope. Skipped when PR #17C already
            # grounded an explicit target (pinned_path set). Two signals,
            # combined conservatively (block if either says vague):
            #   1. deterministic guard on the raw text (no LLM)
            #   2. the LLM fallback's specificity verdict, but ONLY when the
            #      intent itself came from that same LLM call (no extra call)
            if pinned_path is None:
                specificity = assess_implementation_specificity(
                    feature_description
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
                        missing_details=(
                            llm_missing or specificity.missing_details
                        ),
                        examples=specificity.examples,
                    )

        triage_result = await run_triage(
            run_id=run_id,
            project_id=project_id,
            feature_description=feature_description,
        )
        # PR #17C: pin files_expected to the grounded explicit target (set only
        # for a simple, single-chunk implementation edit). Applied before #9B
        # grounding so the pinned path still flows through the same guards.
        if pinned_path is not None:
            triage_result = _pin_single_chunk_files_expected(
                triage_result,
                pinned_path,
                create_target=create_target_path is not None,
            )
        # PR #9B: ground files_expected against the real repo index before the
        # risk scan and before persisting. Removes invented paths and hardens
        # affected chunks. Applies to both implementation and plan_only.
        triage_result = ground_triage_result_paths(
            project_id,
            triage_result,
            sanctioned_new_paths=(
                {create_target_path} if create_target_path is not None else None
            ),
        )
        triage_result = scan_triage_result(triage_result)
        # PR #22A: reconcile the grounded plan against explicit user file
        # constraints ("only A and B", "do not touch A", "similar to A"). Seeds
        # /caps files_expected to a hard allowlist, drops forbidden files, never
        # auto-adds reference-only or planner-prose-only paths, and hardens
        # chunks ([SCOPE] notes + human review) on any drop/cap/mismatch. The
        # user-constraint logic no-ops when no files are named; the planner
        # prose-vs-scope consistency check always runs. scope_guard is unchanged.
        triage_result = reconcile_file_scope(
            project_id,
            triage_result,
            extract_user_file_constraints(feature_description),
            sanctioned_new_paths=(
                {create_target_path} if create_target_path is not None else None
            ),
        )
        if intent == PLAN_ONLY:
            _create_read_only_run(
                run_id=run_id,
                project_id=project_id,
                feature_description=feature_description,
                intent=PLAN_ONLY,
                status=RunStatus.PLAN_READY,
                summary=triage_result.reasoning,
                chunk_plan=triage_result.model_dump_json(),
                total_chunks=triage_result.total_chunks,
            )
            return ChunkPlanResponse(
                run_id=run_id,
                project_id=project_id,
                chunk_plan_status="none",
                total_chunks=triage_result.total_chunks,
                current_chunk_number=0,
                triage=triage_result,
                chunks=[],
            )

        return create_chunked_run(
            run_id=run_id,
            project_id=project_id,
            feature_description=feature_description,
            triage_result=triage_result,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


# PR #17M: copy shown when a clarification token can no longer be honored. The
# user simply re-submits the original request to get a fresh clarification. No
# run is created.
_CLARIFICATION_INVALID_MESSAGE = (
    "This clarification is no longer valid (it may have expired). Please "
    "re-submit your original request to choose a file again."
)
_CLARIFICATION_UNRECOGNIZED_NOTE = (
    "I couldn't tell which option you meant."
)


def _clarification_unavailable_response() -> JSONResponse:
    """Safe read-only response for an invalid/expired/unsupported token."""
    return _needs_clarification_response(
        message=_CLARIFICATION_INVALID_MESSAGE,
        missing_details=["re-submit your original request to pick a file"],
    )


@router.post("/runs/chunked/clarifications/{clarification_id}/select")
async def select_chunked_run_clarification(
    clarification_id: str,
    request: ClarificationSelectionRequest,
):
    """
    Resolve a user's reply ("1" / "yes 1" / "use README.md") within the candidate
    set carried by ``clarification_id`` (PR #17M).

    Safety: "1" is interpreted ONLY here, only against the signed context's
    candidates — never globally on ``/runs/chunked``. A valid selection rebuilds
    the original request with the chosen exact path and re-enters the SAME core
    as a normal request, so the resolver/grounding/scope/approval guards all run
    again and the selected path is re-validated against the live index (never
    trusted as edit authority). Invalid / expired / unrecognized / project-
    mismatched inputs create no run.
    """
    decoded = decode_clarification_context(clarification_id)
    if decoded.status is not ClarificationDecodeStatus.OK or decoded.context is None:
        logger.info(
            "[CLARIFY-SELECT] Token not usable; no run created. status=%s",
            decoded.status.value,
        )
        return _clarification_unavailable_response()

    context = decoded.context

    # Bind the selection to the project the clarification was issued for. A
    # mismatch is a client error and must not create or touch any run.
    if request.project_id != context.project_id:
        logger.info(
            "[CLARIFY-SELECT] Project mismatch; rejecting. token_project=%s "
            "request_project=%s",
            context.project_id,
            request.project_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Clarification does not belong to this project.",
        )

    result = parse_clarification_selection(
        reply=request.selection,
        candidates=context.candidates,
        recommended_path=context.recommended_path,
    )
    if result.status is not SelectionStatus.SELECTED or result.selected_path is None:
        logger.info(
            "[CLARIFY-SELECT] Unrecognized selection; re-clarifying, no run. "
            "reason=%s",
            result.reason,
        )
        # Re-offer the SAME candidates with a fresh token (resets TTL). Built
        # from the stored original intent so messaging/ranking is identical.
        return _target_ambiguous_response(
            context.alias or "",
            tuple(context.candidates),
            context.original_feature_description,
            context.project_id,
            note=_CLARIFICATION_UNRECOGNIZED_NOTE,
        )

    selected_path = result.selected_path
    rebuilt_feature_description = (
        f"{context.original_feature_description.rstrip().rstrip('.')} "
        f"(use {selected_path})"
    )
    logger.info(
        "[CLARIFY-SELECT] Valid selection; delegating to chunked-run core. "
        "selected_path=%s",
        selected_path,
    )
    # Pass the selected path as the authoritative target (#20B-1). The rebuilt
    # description still carries the user's intent for triage/coder context, but
    # path resolution no longer re-derives the original (possibly wrong-case)
    # alias and loops the clarification. The core re-validates the selected path
    # against the live index before pinning it.
    return await _create_chunked_run_core(
        context.project_id,
        rebuilt_feature_description,
        selected_path=selected_path,
    )


def _augment_plan_with_ack_read_model(plan: ChunkPlanResponse) -> ChunkPlanResponse:
    """
    Overlay the #28F acknowledgement state onto each chunk's display-only
    test_validation (#28G), so the frontend can render the acknowledgement
    prompt/confirmation and pre-disable final approval.

    Read-only and additive: this reuses the gate's own
    ``acknowledgement_state`` (the same logic the final-approval precondition
    consults), so the UI can never disagree with what the backend will enforce.
    It changes no gate, never mutates the chunk plan in the DB, and fails safe —
    a chunk whose ack state cannot be computed keeps the model defaults
    (not_required / no acknowledgement outstanding) rather than 500-ing the read.
    """
    for chunk in plan.chunks:
        validation = chunk.test_validation
        if validation is None:
            continue
        verdict = validation.verdict
        if verdict not in ACK_REQUIRED_VERDICTS:
            # strong/unknown: leave the safe defaults (not_required / False).
            continue
        try:
            current_hash = compute_chunk_diff_hash(plan.run_id, chunk.chunk_number)
            state = acknowledgement_state(
                plan.run_id, chunk.chunk_number, verdict, current_hash
            )
        except Exception:
            # Surfacing must never break the read path; keep defaults.
            continue
        chunk.test_validation = validation.model_copy(update={
            "requires_acknowledgement": True,
            "acknowledgement_status": state,
        })
    return plan


@router.get("/runs/{run_id}/chunks", response_model=ChunkPlanResponse)
def get_chunk_plan_route(run_id: str):
    try:
        return _augment_plan_with_ack_read_model(get_chunk_plan_status(run_id))
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


@router.post("/runs/{run_id}/chunks/{chunk_number}/scope-expansion/{request_id}/reject")
def reject_scope_expansion_route(
    run_id: str,
    chunk_number: int,
    request_id: str,
    request: RejectScopeExpansionRequest,
):
    """
    Reject a pending scope expansion request (#27).

    This is NOT retry and NOT code approval. It transitions the request
    pending -> rejected, records the reason, and settles the run off
    `awaiting_scope_approval` back to a plain failed state. The chunk stays
    `failed`; nothing is retried, committed, approved, or applied, and
    chunks.files_expected is never mutated.

    Error mapping (mutates nothing on failure):
      - unknown request, or request not under this run/chunk -> 404
      - request not pending (incl. double reject)            -> 409
    """
    try:
        _ensure_mutating_run(run_id)
        rejected = reject_scope_expansion_request(
            run_id,
            chunk_number,
            request_id,
            reason=request.reason,
        )
        settle_run_after_scope_expansion_reject(run_id, chunk_number)
        return {
            "status": "scope_expansion_rejected",
            "request": rejected.model_dump(),
        }
    except ScopeExpansionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/{chunk_number}/scope-expansion/{request_id}/approve")
async def approve_scope_expansion_route(
    run_id: str,
    chunk_number: int,
    request_id: str,
    request: ApproveScopeExpansionRequest,
):
    """
    Approve a pending scope expansion request and re-drive the chunk retry under
    the amended effective scope (#27E).

    Scope approval is NOT code approval (design §21): it only authorizes retrying
    under a wider allowlist. A successful expanded retry still pauses at the
    existing awaiting_chunk_approval gate and is committed only later through the
    unchanged approval path — this route never commits and never auto-merges.

    The single idempotent endpoint: a re-click after a crash between
    `approved` and the retry write re-drives the retry rather than rejecting it
    (crash-window idempotency, §14).

    Error mapping (a side-effect-free precheck failure keeps the request pending
    and mutates nothing):
      - unknown request, or request not under this run/chunk -> 404
      - non-pending/non-redrivable, stale, dirty tree,
        not-failed chunk, manual intervention                -> 409
      - approved_files validation / ineligibility (422 reasons) -> 422
      - wrong/missing/undeterminable branch                  -> 409 (verify-only)
    """
    try:
        _ensure_mutating_run(run_id)
        result = await approve_and_retry_scope_expansion(
            run_id,
            chunk_number,
            request_id,
            request.approved_files,
            reason=request.reason,
        )
        # Branch precheck surfaces a side-effect-free retry_ineligible dict; map
        # its status_code (409) exactly as the #26 retry route does.
        if (
            isinstance(result, dict)
            and result.get("status") == "retry_ineligible"
            and isinstance(result.get("status_code"), int)
        ):
            return JSONResponse(status_code=result["status_code"], content=result)
        return result
    # ScopeExpansionValidationError is a ValueError subclass; it MUST be caught
    # before the generic ValueError (404) so validation maps to 422.
    except ScopeExpansionValidationError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except ScopeExpansionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/chunks/{chunk_number}/retry")
async def retry_chunk_route(
    run_id: str,
    chunk_number: int,
    request: RetryChunkRequest,
):
    try:
        _ensure_mutating_run(run_id)
        result = await retry_failed_chunk(
            run_id,
            chunk_number,
            request.failure_report_id,
        )
        if (
            isinstance(result, dict)
            and result.get("status") == "retry_ineligible"
            and isinstance(result.get("status_code"), int)
        ):
            return JSONResponse(
                status_code=result["status_code"],
                content=result,
            )
        return result
    except ProjectRepoLockError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


def _build_ack_required_message(blocking) -> str:
    """Human-facing 409 message for the final-approval acknowledgement gate."""
    parts = []
    for item in blocking:
        state = "stale" if item.state == "stale" else "missing"
        parts.append(
            f"chunk {item.chunk_number} ({item.verdict}, acknowledgement {state})"
        )
    detail = "; ".join(parts)
    return (
        "Weak or no-test runtime validation requires acknowledgement before "
        f"final approval: {detail}. Acknowledge each via "
        "POST /runs/{run_id}/chunks/{chunk_number}/test-validation/acknowledge "
        "against the current diff, then approve. Nothing was committed."
    )


def _require_test_validation_acknowledged(run_id: str) -> None:
    """
    #28F final-approval precondition. If any chunk about to be finally approved
    has a weak/none runtime verdict without an ACTIVE acknowledgement bound to its
    CURRENT diff hash, block with 409 BEFORE the final-approved mutation (and so
    before any downstream push/PR, which require FINAL_APPROVED).

    Only enforced when a pending final gate exists, so non-final states keep their
    existing 404. Strong/unknown/no-verdict chunks never block. Reads only;
    mutates nothing on the blocking path.
    """
    if _get_pending_final_gate(run_id) is None:
        return
    blocking = chunks_requiring_acknowledgement(run_id)
    decision = evaluate_final_approval_ack_eligibility(blocking)
    if not decision.eligible:
        raise HTTPException(
            status_code=decision.status_code or 409,
            detail=_build_ack_required_message(decision.blocked_requirements),
        )


@router.post(
    "/runs/{run_id}/chunks/{chunk_number}/test-validation/acknowledge"
)
def acknowledge_test_validation_route(
    run_id: str,
    chunk_number: int,
    request: AcknowledgeTestValidationRequest,
):
    """
    Record a human acknowledgement of a weak/none runtime test verdict for a
    chunk (#28F), bound to the chunk's current diff hash.

    This is NOT code approval and does NOT replace final approval — it is only a
    precondition the final gate checks. It commits nothing, pushes nothing, and
    creates no PR.

    Error mapping (mutates nothing on failure):
      - unknown run/chunk                                  -> 404
      - verdict is strong/unknown or no verdict recorded   -> 409 (nothing to ack)
      - current diff identity cannot be determined         -> 409
      - re-acknowledging the same diff                     -> 200 (idempotent)
    """
    try:
        _ensure_mutating_run(run_id)
        # 404 if the chunk does not exist under this run.
        get_chunk(run_id, chunk_number)

        stored = get_chunk_test_run_verdict(run_id, chunk_number)
        verdict = stored["verdict"] if stored else None
        if verdict not in ACK_REQUIRED_VERDICTS:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This chunk does not require acknowledgement "
                    f"(verdict={verdict or 'none recorded'}). Only weak/no-test "
                    "verdicts can be acknowledged."
                ),
            )

        diff_hash = compute_chunk_diff_hash(run_id, chunk_number)
        if not diff_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot determine the current diff identity for this chunk; "
                    "no acknowledgement was recorded."
                ),
            )

        ack = create_acknowledgement(
            run_id,
            chunk_number,
            verdict,
            diff_hash,
            reason=request.reason,
        )
        return {
            "status": "test_validation_acknowledged",
            "run_id": run_id,
            "chunk_number": chunk_number,
            "verdict": verdict,
            "acknowledged_diff_hash": ack["acknowledged_diff_hash"],
            "acknowledged_at": ack.get("acknowledged_at"),
        }
    except HTTPException:
        raise
    except TestValidationAckConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/final-approval/approve")
def approve_final_approval_route(run_id: str):
    try:
        _ensure_mutating_run(run_id)
        # #28F: block final approval (and so downstream push/PR) when a weak/none
        # verdict is not acknowledged against the current diff. Runs BEFORE the
        # final-approved mutation; raises 409 and mutates nothing if missing/stale.
        _require_test_validation_acknowledged(run_id)
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


@router.post("/runs/{run_id}/memory-conflict/approve")
def approve_memory_conflict_route(run_id: str):
    """
    Override-once: approve the pending DB memory-conflict gate for this run. The run
    returns to an executable state; re-call execute/resume to continue (the gate
    re-evaluation honors the approved override for the same conflict).
    """
    try:
        _ensure_mutating_run(run_id)
        return _decide_memory_conflict_gate(
            run_id,
            ApprovalStatus.APPROVED,
            RunStatus.CHUNK_PLAN_APPROVED,
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/runs/{run_id}/memory-conflict/reject")
def reject_memory_conflict_route(
    run_id: str,
    request: RejectMemoryConflictRequest,
):
    """
    Reject the pending DB memory-conflict gate. The run is rejected before any
    branch/patch/commit/push.
    """
    try:
        _ensure_mutating_run(run_id)
        return _decide_memory_conflict_gate(
            run_id,
            ApprovalStatus.REJECTED,
            RunStatus.REJECTED,
            request.reason or "Memory conflict gate rejected",
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
