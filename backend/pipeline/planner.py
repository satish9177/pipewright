"""
planner.py
First pipeline stage. Receives a feature description,
reads project memory, calls Gemini, returns a validated
PlannerHandoff Pydantic object.

Rules:
  - Never pass API key as argument - load from settings
  - Model selection is resolved per role by role_config (complete_for_role)
  - Always set temperature=0.2 for structured output
  - Always log token usage after every API call
  - Never silently swallow exceptions
  - Retry once on validation failure before raising
  - Always checkpoint after successful plan
"""

import json
import logging
from pydantic import ValidationError

from backend.llm import complete_for_role, log_token_usage
from backend.llm.base import LLMRequest, Message
from backend.llm.retry import ProviderRetryExhaustedError, call_with_rate_limit_retry
from backend.llm.role_config import Role
from backend.models.handoff import PlannerHandoff
from backend.memory.injection_store import capture_memory_injection
from backend.memory.prompt_builder import build_project_memory_block_detailed
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.pipeline.llm_call_provenance_store import try_record_llm_call_provenance
from backend.utils.json_helpers import clean_json_response

PLANNER_TEMPERATURE = 0.2
PLANNER_MAX_TOKENS = 2000

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior engineering planner.
Your job is to analyze a feature request and produce
a precise implementation plan.

You must respond ONLY with a valid JSON object.
No markdown. No backticks. No explanation before or after.
Just the raw JSON object itself.

The JSON must match this exact schema:
{
  "handoff_from": "planner",
  "handoff_to": "coder",
  "run_id": "<the run_id provided>",
  "feature_description": "<the feature request>",
  "goal": "<one clear sentence describing what will be built>",
  "steps": ["step 1", "step 2", "step 3"],
  "files_to_create": ["relative/path/to/new_file.py"],
  "files_to_modify": ["relative/path/to/existing_file.py"],
  "files_to_read": ["relative/path/to/context_file.py"],
  "out_of_scope": ["what this task must not touch"],
  "risks": ["potential risk or edge case"],
  "suggested_memory_entries": ["fact worth storing in project memory"]
}

All paths must be relative to the project root.
steps must have at least 2 entries.
goal must be exactly one sentence.
Respond with nothing except the JSON object."""


def _build_user_prompt(
    feature_description: str,
    run_id: str,
    project_memory_block: str = "",
) -> str:
    memory_section = (
        f"{project_memory_block}\n\n"
        "Remember: Project memory is advisory. Current source code and "
        "explicit user instructions win on conflict.\n\n"
        if project_memory_block
        else ""
    )
    return (
        f"{memory_section}"
        f"FEATURE REQUEST:\n{feature_description}\n\n"
        f"RUN ID: {run_id}\n\n"
        f"Respond with the JSON plan only."
    )


def _build_llm_request(prompt: str) -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content=SYSTEM_PROMPT, cache=True),
            Message(role="user", content=prompt),
        ],
        model="",  # resolved per role by complete_for_role
        temperature=PLANNER_TEMPERATURE,
        max_output_tokens=PLANNER_MAX_TOKENS,
        response_format="json_object",
    )


def _build_correction_request(
    user_prompt: str,
    raw_text: str,
    first_error: Exception,
) -> LLMRequest:
    correction_prompt = (
        f"Your previous response was not valid JSON "
        f"or did not match the required schema.\n\n"
        f"Previous response was:\n{raw_text}\n\n"
        f"Error: {first_error}\n\n"
        f"Please respond again with ONLY the raw JSON. "
        f"No markdown. No explanation. Just the JSON object."
    )
    return LLMRequest(
        messages=[
            Message(role="system", content=SYSTEM_PROMPT, cache=True),
            Message(role="user", content=user_prompt),
            Message(role="assistant", content=raw_text),
            Message(role="user", content=correction_prompt),
        ],
        model="",  # resolved per role by complete_for_role
        temperature=PLANNER_TEMPERATURE,
        max_output_tokens=PLANNER_MAX_TOKENS,
        response_format="json_object",
    )


async def _call_llm(
    request: LLMRequest,
    run_id: str,
    chunk_number: int = 0,
) -> str:
    # Bounded rate-limit retry (E4): backoff + jitter, Retry-After honored —
    # never a 60s sleep while the project repo lock is held. Non-rate-limit
    # errors propagate unchanged.
    response = await call_with_rate_limit_retry(
        lambda: complete_for_role(Role.PLANNER, request),
        description="planner LLM call",
    )
    log_token_usage(response, run_id=run_id, role=Role.PLANNER)
    try_record_llm_call_provenance(
        run_id=run_id,
        chunk_number=chunk_number,
        role=Role.PLANNER.value,
        provider=response.provider,
        model=response.model,
        selection_source=None,
        finish_reason=response.finish_reason,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
    return response.text


def _parse_handoff(raw_text: str, run_id: str) -> PlannerHandoff:
    """
    Clean raw Gemini response and parse into PlannerHandoff.
    Raises ValidationError if schema does not match.
    """
    cleaned = clean_json_response(raw_text)
    data = json.loads(cleaned)
    data["run_id"] = run_id
    return PlannerHandoff.model_validate(data)


async def run_planner(
    feature_description: str,
    run_id: str,
    chunk_number: int = 0,
    project_id: str | None = None,
    project_name: str | None = None,
) -> PlannerHandoff:
    """
    Run the planning stage of the pipeline.

    Loads project memory, calls Gemini with structured
    output prompt, validates response as PlannerHandoff,
    saves checkpoint, and returns the handoff object.

    Retries once if Gemini returns invalid JSON or
    schema mismatch. Raises RuntimeError after 2 failures.
    """
    logger.info("[PLANNER] Starting | run_id=%s", run_id)

    memory_result = build_project_memory_block_detailed(
        project_id=project_id,
        role="planner",
        project_name=project_name,
    )
    project_memory_block = memory_result.block
    # Best-effort provenance capture from the SAME computation. Never raises;
    # failure must not change the prompt or run outcome.
    capture_memory_injection(
        memory_result,
        run_id=run_id,
        project_id=project_id,
        role="planner",
        chunk_number=chunk_number,
    )
    fact_count = len([
        line for line in project_memory_block.splitlines()
        if line.startswith("[")
    ])
    logger.info("[PLANNER] Loaded %s memory facts", fact_count)

    user_prompt = _build_user_prompt(
        feature_description, run_id, project_memory_block
    )
    request = _build_llm_request(user_prompt)

    raw_text = ""

    # First attempt
    try:
        logger.info("[PLANNER] Calling LLM (attempt 1)...")
        raw_text = await _call_llm(request, run_id, chunk_number=chunk_number)
        handoff = _parse_handoff(raw_text, run_id)
        logger.info("[PLANNER] Plan validated on attempt 1")

    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        logger.warning("[PLANNER] Attempt 1 failed: %s", first_error)
        logger.info("[PLANNER] Retrying with correction prompt...")
        correction_request = _build_correction_request(
            user_prompt,
            raw_text,
            first_error,
        )

        try:
            raw_text = await _call_llm(
                correction_request,
                run_id,
                chunk_number=chunk_number,
            )
            handoff = _parse_handoff(raw_text, run_id)
            logger.info("[PLANNER] Plan validated on attempt 2")

        except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
            raise RuntimeError(
                f"planner.py: LLM failed to return valid plan "
                f"after 2 attempts. "
                f"run_id={run_id} | "
                f"error={second_error}"
            )

    except ProviderRetryExhaustedError as exhausted:
        # _call_llm already retried with bounded backoff (E4).
        raise RuntimeError(
            f"planner.py: Rate limited; retries exhausted. "
            f"run_id={run_id} | error={exhausted}"
        )

    except Exception as unexpected:
        raise RuntimeError(
            f"planner.py: Unexpected error during planning. "
            f"run_id={run_id} | error={unexpected}"
        )

    # Save checkpoint after successful plan
    try:
        save_checkpoint(
            run_id=run_id,
            step="plan",
            output=handoff.model_dump(),
            handoff_contract=handoff.model_dump(),
            git_hash="pre-code",
            tests_passed=False,
            step_completed=True,
            chunk_number=chunk_number
        )
        logger.info("[PLANNER] Checkpoint saved | run_id=%s", run_id)
    except Exception as cp_error:
        raise RuntimeError(
            f"planner.py: Failed to save checkpoint. "
            f"run_id={run_id} | error={cp_error}"
        )

    logger.info("[PLANNER] Complete | run_id=%s", run_id)
    return handoff
