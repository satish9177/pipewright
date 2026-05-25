"""
planner.py
First pipeline stage. Receives a feature description,
reads project memory, calls Gemini, returns a validated
PlannerHandoff Pydantic object.

Rules:
  - Never pass API key as argument - load from settings
  - Always pin model version to PLANNER_MODEL constant
  - Always set temperature=0.2 for structured output
  - Always log token usage after every API call
  - Never silently swallow exceptions
  - Retry once on validation failure before raising
  - Always checkpoint after successful plan
"""

import json
import uuid
import asyncio
import google.generativeai as genai
from pydantic import ValidationError

from backend.config.keys import settings
from backend.models.handoff import PlannerHandoff
from backend.memory.memory_store import load_hard_facts
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.utils.json_helpers import clean_json_response

PLANNER_MODEL = "gemini-2.5-flash-lite"
PLANNER_TEMPERATURE = 0.2
PLANNER_MAX_TOKENS = 2000

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
    hard_facts: str
) -> str:
    context = hard_facts if hard_facts else (
        "No memory entries yet. This is a fresh project."
    )
    return (
        f"PROJECT CONTEXT (from memory):\n{context}\n\n"
        f"FEATURE REQUEST:\n{feature_description}\n\n"
        f"RUN ID: {run_id}\n\n"
        f"Respond with the JSON plan only."
    )


def _log_token_usage(response, run_id: str) -> None:
    try:
        usage = response.usage_metadata
        print(
            f"[PLANNER] Token usage | "
            f"run_id={run_id} | "
            f"model={PLANNER_MODEL} | "
            f"input={usage.prompt_token_count} | "
            f"output={usage.candidates_token_count}"
        )
    except Exception:
        print(
            f"[PLANNER] Token usage unavailable | "
            f"run_id={run_id}"
        )


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
    chunk_number: int = 0
) -> PlannerHandoff:
    """
    Run the planning stage of the pipeline.

    Loads project memory, calls Gemini with structured
    output prompt, validates response as PlannerHandoff,
    saves checkpoint, and returns the handoff object.

    Retries once if Gemini returns invalid JSON or
    schema mismatch. Raises RuntimeError after 2 failures.
    """
    print(f"[PLANNER] Starting | run_id={run_id}")

    hard_facts = load_hard_facts()
    fact_count = len(hard_facts.splitlines()) if hard_facts else 0
    print(f"[PLANNER] Loaded {fact_count} memory facts")

    # Configure Gemini client
    genai.configure(api_key=settings.gemini_api_key)

    generation_config = genai.GenerationConfig(
        temperature=PLANNER_TEMPERATURE,
        max_output_tokens=PLANNER_MAX_TOKENS
    )

    model = genai.GenerativeModel(
        model_name=PLANNER_MODEL,
        generation_config=generation_config,
        system_instruction=SYSTEM_PROMPT
    )

    user_prompt = _build_user_prompt(
        feature_description, run_id, hard_facts
    )

    raw_text = ""

    # First attempt
    try:
        print(f"[PLANNER] Calling Gemini (attempt 1)...")
        response = model.generate_content(user_prompt)
        raw_text = response.text
        _log_token_usage(response, run_id)
        handoff = _parse_handoff(raw_text, run_id)
        print(f"[PLANNER] Plan validated on attempt 1")

    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        print(f"[PLANNER] Attempt 1 failed: {first_error}")
        print(f"[PLANNER] Retrying with correction prompt...")

        correction_prompt = (
            f"{user_prompt}\n\n"
            f"Your previous response was not valid JSON "
            f"or did not match the required schema.\n\n"
            f"Previous response was:\n{raw_text}\n\n"
            f"Error: {first_error}\n\n"
            f"Please respond again with ONLY the raw JSON. "
            f"No markdown. No explanation. Just the JSON object."
        )

        try:
            response = model.generate_content(correction_prompt)
            raw_text = response.text
            _log_token_usage(response, run_id)
            handoff = _parse_handoff(raw_text, run_id)
            print(f"[PLANNER] Plan validated on attempt 2")

        except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
            raise RuntimeError(
                f"planner.py: Gemini failed to return valid plan "
                f"after 2 attempts. "
                f"run_id={run_id} | "
                f"error={second_error}"
            )

    except Exception as unexpected:
        error_str = str(unexpected)
        if "429" in error_str:
           print(f"[PLANNER] Rate limited by Gemini. Waiting 60 seconds...")
           await asyncio.sleep(60)
           print(f"[PLANNER] Retrying after rate limit wait...")
           try:
              response = model.generate_content(user_prompt)
              raw_text = response.text
              _log_token_usage(response, run_id)
              handoff = _parse_handoff(raw_text, run_id)
              print(f"[PLANNER] Plan validated after rate limit retry")
           except Exception as retry_error:
              raise RuntimeError(
                f"planner.py: Failed after rate limit retry. "
                f"run_id={run_id} | error={retry_error}"
            )
        else:
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
        print(f"[PLANNER] Checkpoint saved | run_id={run_id}")
    except Exception as cp_error:
        raise RuntimeError(
            f"planner.py: Failed to save checkpoint. "
            f"run_id={run_id} | error={cp_error}"
        )

    print(f"[PLANNER] Complete | run_id={run_id}")
    return handoff
