"""
coder.py
Second pipeline stage. Receives a PlannerHandoff,
reads relevant target repo files, calls the configured LLM provider, and
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
import asyncio
import logging
from pathlib import Path
from pydantic import ValidationError

from backend.llm import complete_for_role, log_token_usage
from backend.llm.base import LLMRequest, LLMResponse, Message
from backend.llm.errors import ProviderRateLimitError
from backend.llm.role_config import Role
from backend.models.handoff import PlannerHandoff, CoderHandoff
from backend.memory.injection_store import capture_memory_injection
from backend.memory.prompt_builder import build_project_memory_block_detailed
from backend.checkpoint.checkpoint_store import save_checkpoint
from backend.pipeline.llm_call_provenance_store import try_record_llm_call_provenance
from backend.utils.json_helpers import clean_json_response
from backend.utils.path_safety import normalize_relative_path, validate_safe_relative_path
from backend.projects.project_context import get_target_repo_path

CODER_MODEL = "gemini-2.5-flash-lite"
CODER_TEMPERATURE = 0.2
CODER_MAX_TOKENS = 8000
CODER_TIMEOUT_SECONDS = 120
MAX_FILE_LINES = 200
# Absolute cap for including a modify target as edit grounding context. A file
# between MAX_FILE_LINES and this cap may not be rewritten wholesale, but is
# still small enough to include in full so the model can locate the exact text
# to target with action="edit". Beyond this cap we refuse rather than truncate.
LARGE_FILE_CONTEXT_LINE_CAP = 1500

logger = logging.getLogger(__name__)

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
      "path": "relative/path/to/new_file.py",
      "action": "create",
      "content": "full file content here as a string",
      "reason": "why this file is created"
    },
    {
      "path": "relative/path/to/existing_file.py",
      "action": "edit",
      "old_string": "the exact text to find (must appear exactly once)",
      "new_string": "the replacement text",
      "reason": "why this change is made"
    }
  ],
  "summary": "one paragraph describing what was implemented",
  "suggested_memory_entries": ["fact worth storing"]
}

Action must be one of: create / modify / delete / edit
All file paths must be relative to the project root.

Choosing the action:
- create: a brand new file. Provide complete "content".
- edit: a targeted change to an EXISTING file. Provide "old_string" and
  "new_string" instead of "content". This is the PREFERRED way to change an
  existing file. old_string must be copied verbatim from the file shown to you
  and must match EXACTLY ONE location (include enough surrounding text to make
  it unique). Do not provide "content" for an edit.
- modify: full-file replacement of a SMALL existing file (200 lines or fewer).
  Provide complete "content". For files over 200 lines you MUST use "edit";
  full-content "modify" of a large file will be rejected.
- delete: remove a file. content should be null.

Rules:
- For a small change to an existing file, prefer "edit" over "modify".
- For any file marked LARGE FILE in the context, you MUST use "edit".
- File "content" must always be the complete file content, never partial.
- Never truncate with comments like # rest of file here.
- Never return partial file content as "content".
Respond with nothing except the JSON object."""


def _read_target_file(
    relative_path: str,
    target_repo: str,
    max_lines: int = 200,
    purpose: str = "modify",
) -> str | None:
    """
    Read a file from target repo safely.
    Returns file content string or None if file not found.
    Raises RuntimeError if path traversal detected.
    Refuses files larger than max_lines to avoid unsafe full-file rewrites.
    """
    try:
        root = Path(target_repo).resolve()
        file_path = validate_safe_relative_path(relative_path, root)

        if not file_path.exists():
            logger.warning("[CODER] Warning: target file missing, skipping %s", relative_path)
            return None

        if not file_path.is_file():
            logger.warning("[CODER] Warning: target path is not a file, skipping %s", relative_path)
            return None

        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)

        if purpose == "read" and len(lines) > max_lines:
            raise RuntimeError(
                f"Refusing to read large file {relative_path}: "
                f"{len(lines)} lines exceeds safe limit {max_lines}. "
                "Large-file reading requires explicit summarization."
            )

        # Modify targets larger than max_lines are NOT refused outright. They are
        # still included as context so the model can ground a targeted
        # action="edit" (old_string/new_string). Wholesale full-content rewrites
        # of these files are blocked downstream in patch_applier. We only refuse
        # when the file is too large to include safely at all.
        if purpose != "read" and len(lines) > LARGE_FILE_CONTEXT_LINE_CAP:
            raise RuntimeError(
                f"File is too large for full context. Use a more specific "
                f"request or targeted search context. "
                f"({relative_path}: {len(lines)} lines exceeds "
                f"{LARGE_FILE_CONTEXT_LINE_CAP})"
            )

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
        path_purposes: dict[str, str] = {}

        for path in files_to_read:
            normalized = normalize_relative_path(path)
            if normalized not in seen:
                seen.add(normalized)
                ordered_paths.append(normalized)
            path_purposes[normalized] = "read"

        for path in files_to_modify:
            normalized = normalize_relative_path(path)
            if normalized not in seen:
                seen.add(normalized)
                ordered_paths.append(normalized)
            path_purposes[normalized] = "modify"

        blocks = []
        for path in ordered_paths:
            purpose = path_purposes.get(path, "modify")
            content = _read_target_file(
                path,
                target_repo,
                MAX_FILE_LINES,
                purpose,
            )
            if content is None:
                continue
            header = f"--- FILE: {path} ---"
            if purpose == "modify" and len(content.splitlines()) > MAX_FILE_LINES:
                header = (
                    f"--- FILE: {path} "
                    f'(LARGE FILE, over {MAX_FILE_LINES} lines — you MUST change '
                    f'this file with action="edit" using old_string/new_string; '
                    f"do NOT return full file content) ---"
                )
            blocks.append(
                f"{header}\n"
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
    project_memory_block: str,
    file_contents_block: str
) -> str:
    memory_section = (
        f"{project_memory_block}\n\n"
        "Remember: Project memory is advisory. Current source code and "
        "explicit user instructions win on conflict.\n"
        if project_memory_block
        else ""
    )
    return f"""
{memory_section}

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


def _build_llm_request(user_prompt: str) -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content=CODER_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ],
        model=CODER_MODEL,
        temperature=CODER_TEMPERATURE,
        max_output_tokens=CODER_MAX_TOKENS,
        timeout_seconds=CODER_TIMEOUT_SECONDS,
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
            Message(role="system", content=CODER_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
            Message(role="assistant", content=raw_text),
            Message(role="user", content=correction_prompt),
        ],
        model=CODER_MODEL,
        temperature=CODER_TEMPERATURE,
        max_output_tokens=CODER_MAX_TOKENS,
        timeout_seconds=CODER_TIMEOUT_SECONDS,
        response_format="json_object",
    )


async def _call_llm(request: LLMRequest, run_id: str) -> LLMResponse:
    response = await complete_for_role(Role.CODER, request)
    log_token_usage(response, run_id=run_id, role=Role.CODER)
    return response


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
    run_id: str,
    chunk_number: int = 0,
    project_id: str | None = None,
    project_name: str | None = None,
) -> CoderHandoff:
    """
    Run the coding stage of the pipeline.

    Loads project memory, reads requested target repo files,
    calls Gemini with structured output prompt, validates
    response as CoderHandoff, saves checkpoint, and returns
    the handoff object.
    """
    logger.info("[CODER] Starting | run_id=%s", run_id)

    try:
        memory_result = build_project_memory_block_detailed(
            project_id=project_id,
            role="coder",
            project_name=project_name,
        )
        project_memory_block = memory_result.block
        # Best-effort provenance capture from the SAME computation. Never raises;
        # a coder re-run (patch retry / scope-expansion retry) produces a new
        # coder event at a higher attempt_number. Failure must not change the
        # prompt or run outcome.
        capture_memory_injection(
            memory_result,
            run_id=run_id,
            project_id=project_id,
            role="coder",
            chunk_number=chunk_number,
        )
        fact_count = len([
            line for line in project_memory_block.splitlines()
            if line.startswith("[")
        ])
        logger.info("[CODER] Loaded %s memory facts", fact_count)

        target_repo = get_target_repo_path()
        logger.info("[CODER] Reading target repo files from %s", target_repo)
        file_contents_block = _build_file_contents_block(
            plan.files_to_read,
            plan.files_to_modify,
            target_repo
        )

        user_prompt = _build_user_prompt(
            plan, run_id, project_memory_block, file_contents_block
        )
        request = _build_llm_request(user_prompt)

        raw_text = ""
        coder_response: LLMResponse | None = None

        try:
            logger.info("[CODER] Calling LLM (attempt 1)...")
            coder_response = await _call_llm(request, run_id)
            raw_text = coder_response.text
            handoff = _parse_handoff(raw_text, run_id)
            logger.info("[CODER] Handoff validated on attempt 1")

        except ValidationError as first_error:
            handoff, coder_response = await _retry_after_parse_failure(
                user_prompt, raw_text, first_error, run_id
            )
        except json.JSONDecodeError as first_error:
            handoff, coder_response = await _retry_after_parse_failure(
                user_prompt, raw_text, first_error, run_id
            )
        except ValueError as first_error:
            handoff, coder_response = await _retry_after_parse_failure(
                user_prompt, raw_text, first_error, run_id
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
            tests_passed=False,
            step_completed=True,
            chunk_number=chunk_number
        )
        logger.info("[CODER] Checkpoint saved | run_id=%s", run_id)
    except Exception as cp_error:
        raise RuntimeError(
            f"coder.py: Failed to save checkpoint. "
            f"run_id={run_id} | error={cp_error}"
        )

    # Metadata-only provenance for the coder's EFFECTIVE output (#33B). Records
    # which provider/model actually produced this handoff. Best-effort and
    # isolated: it stores no prompts/responses/diffs/secrets, it never gates,
    # commits, retries, or expands scope, and a failure here can never change the
    # coder result (try_* swallows and logs). selection_source is left None — it is
    # not derived here so resolve_role_config behavior is untouched.
    if coder_response is not None:
        try_record_llm_call_provenance(
            run_id=run_id,
            chunk_number=chunk_number,
            role=Role.CODER.value,
            provider=coder_response.provider,
            model=coder_response.model,
            selection_source=None,
            finish_reason=coder_response.finish_reason,
            input_tokens=coder_response.input_tokens,
            output_tokens=coder_response.output_tokens,
        )

    logger.info("[CODER] Complete | run_id=%s", run_id)
    return handoff


async def _retry_after_parse_failure(
    user_prompt: str,
    raw_text: str,
    first_error: Exception,
    run_id: str
) -> tuple[CoderHandoff, LLMResponse]:
    logger.warning("[CODER] Attempt 1 failed: %s", first_error)
    logger.info("[CODER] Retrying with correction prompt...")
    correction_request = _build_correction_request(
        user_prompt,
        raw_text,
        first_error,
    )

    try:
        coder_response = await _call_llm(correction_request, run_id)
        handoff = _parse_handoff(coder_response.text, run_id)
        logger.info("[CODER] Handoff validated on attempt 2")
        return handoff, coder_response
    except ValidationError as second_error:
        raise RuntimeError(
            f"coder.py: LLM failed to return valid code handoff "
            f"after 2 attempts. run_id={run_id} | error={second_error}"
        )
    except json.JSONDecodeError as second_error:
        raise RuntimeError(
            f"coder.py: LLM failed to return valid code handoff "
            f"after 2 attempts. run_id={run_id} | error={second_error}"
        )
    except ValueError as second_error:
        raise RuntimeError(
            f"coder.py: LLM failed to return valid code handoff "
            f"after 2 attempts. run_id={run_id} | error={second_error}"
        )
    except Exception as unexpected:
        error_str = str(unexpected)
        if isinstance(unexpected, ProviderRateLimitError) or "429" in error_str:
            logger.warning("[CODER] Rate limited by LLM. Waiting 60 seconds...")
            await asyncio.sleep(60)
            logger.info("[CODER] Retrying after rate limit wait...")
            try:
                coder_response = await _call_llm(
                    _build_llm_request(user_prompt), run_id
                )
                handoff = _parse_handoff(coder_response.text, run_id)
                logger.info("[CODER] Handoff validated after rate limit retry")
                return handoff, coder_response
            except Exception as retry_error:
                raise RuntimeError(
                    f"coder.py: Failed after rate limit retry. "
                    f"run_id={run_id} | error={retry_error}"
                )
        else:
            raise RuntimeError(
                f"coder.py: Unexpected error during coding. "
                f"run_id={run_id} | error={unexpected}"
            )
