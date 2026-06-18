"""
reviewer.py
Internal advisory reviewer execution for the Adversarial Reviewer Stage v1
(design: docs/design/adversarial-reviewer-stage.md).

After a chunk's patch applies, its tests pass, and the runtime test verdict is
persisted, ``run_chunk_review`` produces a single best-effort, advisory AI review of
the standing applied diff + test evidence and stores it in chunk_reviews. It is
DISPLAY-ONLY evidence for a later read/API/UI slice.

Critical invariant: this module is **best-effort and never raises**. The caller's
chunk outcome must be identical whether the reviewer succeeds, fails, times out,
returns malformed JSON, or is unavailable. The reviewer here:

  - gates nothing, commits nothing, approves nothing, rejects nothing, fixes
    nothing, writes no memory, creates no PR;
  - runs a single LLM attempt with a strict request timeout (no retry loop — it may
    run inside the existing repo lock);
  - binds its record to the EXISTING chunk diff/test-checkpoint identity
    (``current_chunk_review_identity`` → ``compute_chunk_diff_hash``), never a new
    hash scheme;
  - on ANY failure stores an ``unavailable`` record (provably empty) and returns,
    swallowing the error after logging a sanitized summary.
"""

import json
import logging
import uuid

from backend.llm import (
    LLMRequest,
    Message,
    Role,
    complete_for_role,
    log_token_usage,
    resolve_role_config,
)
from backend.models.chunk import ChunkDefinition
from backend.models.handoff import CoderHandoff, PatchResult, PipelineTestResult
from backend.pipeline.chunk_review_store import (
    create_review,
    current_chunk_review_identity,
)
from backend.pipeline.chunk_store import get_chunk_test_run_verdict
from backend.pipeline.llm_call_provenance_store import try_record_llm_call_provenance
from backend.pipeline.policy import REVIEWER_MAX_DIFF_CHARS
from backend.pipeline.reviewer_models import (
    ChunkReviewRecord,
    ChunkReviewStatus,
)
from backend.projects.project_store import require_project
from backend.utils.json_helpers import clean_json_response
from backend.utils.path_safety import is_forbidden_write_path

logger = logging.getLogger(__name__)

# Conservative caps so a large diff / test log never bloats the prompt, blows up
# token cost, or becomes an egress vector. Diffs keep the head (where the change
# starts); test output keeps the tail (where the pass/fail summary lives).
# The diff cap is shared policy (policy.py §8b); the rest stay reviewer-local.
REVIEWER_MAX_TEST_OUTPUT_CHARS = 4000
REVIEWER_MAX_FILES_LISTED = 50
REVIEWER_MAX_ERROR_CHARS = 300

REVIEWER_TEMPERATURE = 0.0
REVIEWER_MAX_TOKENS = 2000
# Strict single-attempt timeout via the existing LLM request config. The provider
# honors LLMRequest.timeout_seconds; we never add a retry loop here.
REVIEWER_TIMEOUT_SECONDS = 60

REVIEWER_SYSTEM_PROMPT = """You are an adversarial senior code reviewer.

You are given a feature request, one chunk's task, the files it was allowed to
touch, the files it actually changed, the applied diff, and the test evidence. Your
job is to look critically for: correctness gaps, missing or weak tests, scope
concerns, security/safety concerns, requirement mismatches, and anything a human
should double-check.

You have NO authority. You do not approve, reject, merge, or commit anything. Your
output is advisory evidence for a human reviewer. "approve_with_notes" does NOT mean
"safe to merge"; it means "no blocking concern found, but read the notes".

Only cite files that appear in the provided diff/changed-files. Do not invent files
or issues you cannot ground in the provided evidence. Be terse and specific.

Respond with ONLY a single raw JSON object. No markdown, no backticks, no prose
before or after. The JSON must match exactly:
{
  "verdict": "approve_with_notes | needs_human_attention | risky",
  "summary": "one short paragraph",
  "findings": [
    {
      "category": "correctness | test_gap | scope | security | maintainability | requirement_mismatch | uncertainty",
      "severity": "info | warning | high",
      "title": "short title",
      "explanation": "why this matters",
      "affected_files": ["path/in/diff.py"],
      "suggested_human_check": "what a human should verify",
      "confidence": 0.5
    }
  ],
  "test_gap_summary": "string or empty",
  "scope_summary": "string or empty",
  "security_or_safety_summary": "string or empty",
  "recommended_human_action": "string or empty"
}
findings may be an empty list. Respond with nothing except the JSON object."""


def _cap_head(text: str | None, max_chars: int) -> str:
    """Bound text keeping the HEAD (for diffs). Pure; never raises."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... truncated, kept first {max_chars} chars ...]"


def _cap_tail(text: str | None, max_chars: int) -> str:
    """Bound text keeping the TAIL (for test output summaries). Pure; never raises."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return f"[... truncated, kept last {max_chars} chars ...]\n" + text[-max_chars:]


def _sanitize_diff(patch: PatchResult) -> str:
    """
    Bounded, defense-in-depth-filtered diff for the prompt.

    The diff is already scope-guarded and forbidden-path-guarded upstream
    (patch_applier). As defense-in-depth, if any applied path fails the strict
    write-path safety check, omit the diff entirely rather than risk shipping
    sensitive content to the provider. Otherwise cap to REVIEWER_MAX_DIFF_CHARS.
    """
    try:
        applied = list(getattr(patch, "files_applied", None) or [])
        if any(is_forbidden_write_path(path) for path in applied):
            return "[diff omitted: a changed path failed the path-safety check]"
        return _cap_head(getattr(patch, "diff", "") or "", REVIEWER_MAX_DIFF_CHARS)
    except Exception:
        # Never let input shaping raise; an empty diff just yields a weaker review.
        return ""


def _changed_files(patch: PatchResult) -> list[str]:
    applied = list(getattr(patch, "files_applied", None) or [])
    return applied[:REVIEWER_MAX_FILES_LISTED]


def _verdict_block(run_id: str, chunk_number: int) -> str:
    """
    Best-effort runtime test-validation evidence (#28) for the prompt. Reads only the
    already-persisted chunk verdict columns; never raises.
    """
    try:
        stored = get_chunk_test_run_verdict(run_id, chunk_number)
    except Exception:
        stored = None
    if not stored:
        return "Runtime test verdict: unavailable"
    verdict = stored.get("verdict")
    reason = stored.get("reason") or ""
    return f"Runtime test verdict: {verdict}\nVerdict reason: {reason}".strip()


def _test_command(project_id: str | None) -> str:
    """Best-effort configured test command (no secrets); empty when unknown."""
    if not project_id:
        return ""
    try:
        return require_project(project_id).get("test_command") or ""
    except Exception:
        return ""


def build_reviewer_prompt(
    *,
    run_id: str,
    chunk: ChunkDefinition,
    code: CoderHandoff,
    patch: PatchResult,
    test_result: PipelineTestResult,
    test_command: str,
    verdict_block: str,
) -> str:
    """
    Build the bounded, sanitized user prompt from already-available context. Pure
    (no I/O). Excludes secrets/tokens/full-file-contents/unbounded output by
    construction: only the bounded diff, bounded test output, paths, and the
    feature/chunk descriptions are included.
    """
    files_expected = list(chunk.files_expected or [])
    changed = _changed_files(patch)
    diff = _sanitize_diff(patch)
    test_output = _cap_tail(getattr(test_result, "output", ""), REVIEWER_MAX_TEST_OUTPUT_CHARS)

    return (
        f"FEATURE REQUEST:\n{code.feature_description}\n\n"
        f"CHUNK {chunk.chunk_number}: {chunk.title}\n"
        f"{chunk.description}\n\n"
        f"FILES EXPECTED (approved scope):\n{files_expected}\n\n"
        f"FILES ACTUALLY CHANGED:\n{changed}\n\n"
        f"CODER SUMMARY:\n{code.summary}\n\n"
        f"APPLIED DIFF (bounded):\n{diff}\n\n"
        f"TEST COMMAND:\n{test_command or '(unknown)'}\n\n"
        f"{verdict_block}\n\n"
        f"TEST OUTPUT (bounded):\n{test_output}\n\n"
        f"RUN ID: {run_id}\n\n"
        "Review the change and respond with only the JSON object."
    )


def _build_request(user_prompt: str, model: str) -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content=REVIEWER_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ],
        model=model,
        temperature=REVIEWER_TEMPERATURE,
        max_output_tokens=REVIEWER_MAX_TOKENS,
        timeout_seconds=REVIEWER_TIMEOUT_SECONDS,
        response_format="json_object",
    )


def parse_completed_review(
    raw_text: str,
    *,
    review_id: str,
    run_id: str,
    chunk_number: int,
    reviewed_hash: str | None,
    provider: str | None,
    model: str | None,
) -> ChunkReviewRecord:
    """
    Parse strict reviewer JSON into a COMPLETED ChunkReviewRecord.

    Raises ValueError/JSONDecodeError/ValidationError on malformed or invalid
    output; the caller maps any of these to an ``unavailable`` record. A completed
    record requires a valid verdict, enforced by ChunkReviewRecord.
    """
    data = json.loads(clean_json_response(raw_text))
    return ChunkReviewRecord.model_validate({
        "id": review_id,
        "run_id": run_id,
        "chunk_number": chunk_number,
        "review_status": ChunkReviewStatus.COMPLETED,
        "verdict": data.get("verdict"),
        "summary": data.get("summary"),
        "findings": data.get("findings") or [],
        "test_gap_summary": data.get("test_gap_summary"),
        "scope_summary": data.get("scope_summary"),
        "security_or_safety_summary": data.get("security_or_safety_summary"),
        "recommended_human_action": data.get("recommended_human_action"),
        "reviewed_test_checkpoint_hash": reviewed_hash,
        "provider": provider,
        "model": model,
    })


def _store_unavailable(
    *,
    review_id: str,
    run_id: str,
    chunk_number: int,
    reviewed_hash: str | None,
    provider: str | None,
    model: str | None,
) -> ChunkReviewRecord | None:
    """
    Persist a provably-empty ``unavailable`` record (no verdict/findings/summaries).
    Best-effort: if even storing fails, swallow and return None. Stores NO error
    text — the failure detail is only logged, sanitized, by the caller.
    """
    try:
        record = ChunkReviewRecord(
            id=review_id,
            run_id=run_id,
            chunk_number=chunk_number,
            review_status=ChunkReviewStatus.UNAVAILABLE,
            reviewed_test_checkpoint_hash=reviewed_hash,
            provider=provider,
            model=model,
        )
        return create_review(record)
    except Exception as error:
        logger.warning(
            "[REVIEWER] could not store unavailable review | run_id=%s | chunk=%s | "
            "error=%s",
            run_id, chunk_number, _safe_error(error),
        )
        return None


def _safe_error(error: Exception) -> str:
    """Bounded error text for logs only (never stored/returned)."""
    return str(error)[:REVIEWER_MAX_ERROR_CHARS]


async def run_chunk_review(
    *,
    run_id: str,
    project_id: str | None,
    chunk: ChunkDefinition,
    code: CoderHandoff,
    patch: PatchResult,
    test_result: PipelineTestResult,
) -> ChunkReviewRecord | None:
    """
    Produce and store one best-effort advisory review for a chunk whose patch
    applied and whose tests passed. NEVER raises; always returns the stored record
    (completed or unavailable) or None if even the unavailable write failed.

    Caller contract: invoke ONLY after patch apply succeeded, tests passed, and the
    verdict was persisted — i.e. there is a standing applied diff. Do not call on a
    patch-apply failure or a rolled-back test failure.
    """
    chunk_number = chunk.chunk_number
    review_id = str(uuid.uuid4())

    # Bind to the existing chunk diff/test-checkpoint identity (#28F helper). Best
    # effort: an indeterminate identity is fine (None) — it reads STALE later, never
    # falsely current.
    try:
        reviewed_hash = current_chunk_review_identity(run_id, chunk_number)
    except Exception:
        reviewed_hash = None

    # Resolve the attempted reviewer provider/model for metadata (names only, no
    # secrets). Best-effort; falls back to None.
    attempted_provider: str | None = None
    attempted_model: str | None = None
    try:
        config = resolve_role_config(Role.REVIEWER)
        attempted_provider, attempted_model = config.provider, config.model
    except Exception:
        attempted_provider, attempted_model = None, None

    try:
        prompt = build_reviewer_prompt(
            run_id=run_id,
            chunk=chunk,
            code=code,
            patch=patch,
            test_result=test_result,
            test_command=_test_command(project_id),
            verdict_block=_verdict_block(run_id, chunk_number),
        )
        request = _build_request(prompt, attempted_model or "reviewer")
        response = await complete_for_role(Role.REVIEWER, request)
        log_token_usage(response, run_id=run_id, role=Role.REVIEWER)
        try_record_llm_call_provenance(
            run_id=run_id,
            chunk_number=chunk_number,
            role=Role.REVIEWER.value,
            provider=response.provider,
            model=response.model,
            selection_source=None,
            finish_reason=response.finish_reason,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

        record = parse_completed_review(
            response.text,
            review_id=review_id,
            run_id=run_id,
            chunk_number=chunk_number,
            reviewed_hash=reviewed_hash,
            provider=response.provider,
            model=response.model,
        )
        stored = create_review(record)
        logger.info(
            "[REVIEWER] advisory review stored | run_id=%s | chunk=%s | verdict=%s",
            run_id, chunk_number, stored.verdict.value if stored.verdict else "none",
        )
        return stored
    except Exception as error:
        # Any failure (provider error, timeout, malformed JSON, invalid model
        # output, storage error) => unavailable. Never re-raised; never affects the
        # chunk outcome.
        logger.warning(
            "[REVIEWER] advisory review unavailable | run_id=%s | chunk=%s | error=%s",
            run_id, chunk_number, _safe_error(error),
        )
        return _store_unavailable(
            review_id=review_id,
            run_id=run_id,
            chunk_number=chunk_number,
            reviewed_hash=reviewed_hash,
            provider=attempted_provider,
            model=attempted_model,
        )
