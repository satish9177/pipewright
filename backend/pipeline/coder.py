"""
coder.py
Second pipeline stage. Receives a PlannerHandoff,
reads relevant target repo files, calls Gemini, and
returns a validated CoderHandoff Pydantic object.

Rules:
  - Coder never writes files to disk
  - Never pass API key as argument - load from settings
  - Always pin model version to CODER_MODEL constant
  - Always set temperature=0.2 for structured output
  - Always log token usage after every API call
  - Never silently swallow exceptions
  - Retry once on validation failure before raising
  - Always checkpoint after successful structured output
"""

import json
import os
from pathlib import Path
from pydantic import ValidationError
import google.generativeai as genai

from backend.config.keys import settings
from backend.models.handoff import PlannerHandoff, CoderHandoff
from backend.memory.memory_store import load_hard_facts
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.utils.json_helpers import clean_json_response

CODER_MODEL = "gemini-2.5-flash"
CODER_TEMPERATURE = 0.2
CODER_MAX_TOKENS = 8000
CODER_TIMEOUT_SECONDS = 120
MAX_FILE_LINES = 200

CODER_SYSTEM_PROMPT = """You are a senior software engineer.
Your job is to implement a precise plan by writing
or modifying code files.

You must respond ONLY with a valid JSON object.
No markdown. No backticks. No explanation before or after.
Just the raw JSON object itself.

The JSON must match this exact schema:
{
  "handoff_from": "coder",
  "handoff_to": "patch_applier",
  "run_id": "<the run_id provided>",
  "feature_description": "<the feature>",
  "files_changed": [
    {
      "path": "relative/path/to/file.py",
      "action": "create",
      "content": "full file content here as a string",
      "reason": "why this file is created or modified"
    }
  ],
  "summary": "one paragraph describing what was implemented",
  "suggested_memory_entries": ["fact worth storing"]
}

Action must be one of: create / modify / delete
For delete action content should be null.
All file paths must be relative to the project root.
File content must be the complete file content.
Never return partial file content.
Never truncate with comments like # rest of file here.
Respond with nothing except the JSON object."""


def _read_target_file(
    relative_path: str,
    target_repo: str,
    max_lines: int = 200
) -> str | None:
    """
    Read a file from target repo safely.
    Returns file content string or None if file not found.
    Raises RuntimeError if path traversal detected.
    Truncates to max_lines with warning if file is longer.
    """
    try:
        root = Path(target_repo).resolve()
        file_path = (root / relative_path).resolve()

        try:
            file_path.relative_to(root)
        except ValueError:
            raise RuntimeError(
                f"coder.py: path traversal detected for {relative_path}"
            )

        if not file_path.exists():
            print(f"[CODER] Warning: target file missing, skipping {relative_path}")
            return None

        if not file_path.is_file():
            print(f"[CODER] Warning: target path is not a file, skipping {relative_path}")
            return None

        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > max_lines:
            print(
                f"[CODER] Warning: truncating {relative_path} "
                f"from {len(lines)} lines to {max_lines} lines"
            )
            lines = lines[:max_lines]

        return "".join(lines)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"coder.py: failed to read target file {relative_path}: {error}"
        )


def _build_file_contents_block(
    files_to_read: list[str],
    files_to_modify: list[str],
    target_repo: str
) -> str:
    """
    Read all relevant files and build
    the context block for the prompt.
    Deduplicates file paths.
    Skips missing files with a warning.
    Returns formatted string block.
    """
    try:
        seen = set()
        ordered_paths = []

        for path in [*files_to_read, *files_to_modify]:
            normalized = os.path.normpath(path)
            if normalized not in seen:
                seen.add(normalized)
                ordered_paths.append(path)

        blocks = []
        for path in ordered_paths:
            content = _read_target_file(path, target_repo, MAX_FILE_LINES)
            if content is None:
                continue
            blocks.append(
                f"--- FILE: {path} ---\n"
                f"{content}\n"
                f"--- END FILE ---"
            )

        return "\n\n".join(blocks) if blocks else "No existing files were read."
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"coder.py: failed to build file contents block: {error}"
        )


def _build_user_prompt(
    plan: PlannerHandoff,
    run_id: str,
    hard_facts: str,
    file_contents_block: str
) -> str:
    return f"""
PROJECT CONTEXT (from memory):
{hard_facts if hard_facts else "No memory entries yet."}

IMPLEMENTATION PLAN:
Goal: {plan.goal}
Steps:
{chr(10).join(f"  {i+1}. {s}" for i, s in enumerate(plan.steps))}

Files to create: {plan.files_to_create}
Files to modify: {plan.files_to_modify}
Out of scope: {plan.out_of_scope}
Risks to watch: {plan.risks}

EXISTING FILE CONTENTS:
{file_contents_block}

RUN ID: {run_id}

Implement the plan exactly.
Return only the JSON response.
"""


def _log_token_usage(response, run_id: str) -> None:
    try:
        usage = response.usage_metadata
        print(
            f"[CODER] Token usage | "
            f"run_id={run_id} | "
            f"model={CODER_MODEL} | "
            f"input={usage.prompt_token_count} | "
            f"output={usage.candidates_token_count}"
        )
    except Exception:
        print(
            f"[CODER] Token usage unavailable | "
            f"run_id={run_id}"
        )


def _parse_handoff(raw_text: str, run_id: str) -> CoderHandoff:
    """
    Clean raw Gemini response and parse into CoderHandoff.
    Raises ValidationError if schema does not match.
    """
    cleaned = clean_json_response(raw_text)
    data = json.loads(cleaned)
    data["run_id"] = run_id
    return CoderHandoff.model_validate(data)


async def run_coder(
    plan: PlannerHandoff,
    run_id: str
) -> CoderHandoff:
    """
    Run the coding stage of the pipeline.

    Loads project memory, reads requested target repo files,
    calls Gemini with structured output prompt, validates
    response as CoderHandoff, saves checkpoint, and returns
    the handoff object.
    """
    print(f"[CODER] Starting | run_id={run_id}")

    try:
        hard_facts = load_hard_facts()
        fact_count = len(hard_facts.splitlines()) if hard_facts else 0
        print(f"[CODER] Loaded {fact_count} memory facts")

        target_repo = settings.target_repo_path
        print(f"[CODER] Reading target repo files from {target_repo}")
        file_contents_block = _build_file_contents_block(
            plan.files_to_read,
            plan.files_to_modify,
            target_repo
        )

        genai.configure(api_key=settings.gemini_api_key)

        generation_config = genai.GenerationConfig(
            temperature=CODER_TEMPERATURE,
            max_output_tokens=CODER_MAX_TOKENS
        )

        model = genai.GenerativeModel(
            model_name=CODER_MODEL,
            generation_config=generation_config,
            system_instruction=CODER_SYSTEM_PROMPT
        )

        user_prompt = _build_user_prompt(
            plan, run_id, hard_facts, file_contents_block
        )

        raw_text = ""

        try:
            print("[CODER] Calling Gemini (attempt 1)...")
            response = model.generate_content(
                user_prompt,
                request_options={"timeout": CODER_TIMEOUT_SECONDS}
            )
            raw_text = response.text
            _log_token_usage(response, run_id)
            handoff = _parse_handoff(raw_text, run_id)
            print("[CODER] Handoff validated on attempt 1")

        except ValidationError as first_error:
            handoff = _retry_after_parse_failure(
                model, user_prompt, raw_text, first_error, run_id
            )
        except json.JSONDecodeError as first_error:
            handoff = _retry_after_parse_failure(
                model, user_prompt, raw_text, first_error, run_id
            )
        except ValueError as first_error:
            handoff = _retry_after_parse_failure(
                model, user_prompt, raw_text, first_error, run_id
            )

    except RuntimeError:
        raise
    except Exception as unexpected:
        raise RuntimeError(
            f"coder.py: Unexpected error during coding. "
            f"run_id={run_id} | error={unexpected}"
        )

    try:
        save_checkpoint(
            run_id=run_id,
            step="code",
            output=handoff.model_dump(),
            handoff_contract=handoff.model_dump(),
            git_hash="pre-patch",
            tests_passed=True
        )
        print(f"[CODER] Checkpoint saved | run_id={run_id}")
    except Exception as cp_error:
        raise RuntimeError(
            f"coder.py: Failed to save checkpoint. "
            f"run_id={run_id} | error={cp_error}"
        )

    print(f"[CODER] Complete | run_id={run_id}")
    return handoff


def _retry_after_parse_failure(
    model,
    user_prompt: str,
    raw_text: str,
    first_error: Exception,
    run_id: str
) -> CoderHandoff:
    print(f"[CODER] Attempt 1 failed: {first_error}")
    print("[CODER] Retrying with correction prompt...")

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
        response = model.generate_content(
            correction_prompt,
            request_options={"timeout": CODER_TIMEOUT_SECONDS}
        )
        raw_text = response.text
        _log_token_usage(response, run_id)
        handoff = _parse_handoff(raw_text, run_id)
        print("[CODER] Handoff validated on attempt 2")
        return handoff
    except ValidationError as second_error:
        raise RuntimeError(
            f"coder.py: Gemini failed to return valid code handoff "
            f"after 2 attempts. run_id={run_id} | error={second_error}"
        )
    except json.JSONDecodeError as second_error:
        raise RuntimeError(
            f"coder.py: Gemini failed to return valid code handoff "
            f"after 2 attempts. run_id={run_id} | error={second_error}"
        )
    except ValueError as second_error:
        raise RuntimeError(
            f"coder.py: Gemini failed to return valid code handoff "
            f"after 2 attempts. run_id={run_id} | error={second_error}"
        )
    except Exception as unexpected:
        raise RuntimeError(
            f"coder.py: Unexpected error during retry. "
            f"run_id={run_id} | error={unexpected}"
        )
