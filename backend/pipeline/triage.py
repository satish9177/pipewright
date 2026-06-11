"""
triage.py
Phase 2B chunk planning stage.

Triage turns one feature request into an ordered chunk plan. It does not create
pipeline_runs rows and it never executes chunks.
"""

import asyncio
import json
import logging

from pydantic import ValidationError

from backend.llm import complete_for_role, log_token_usage
from backend.llm.base import LLMRequest, Message
from backend.llm.role_config import Role
from backend.memory.injection_store import capture_memory_injection
from backend.memory.prompt_builder import build_project_memory_block_detailed
from backend.models.chunk import TriageResult
from backend.projects.project_store import require_project
from backend.repo.index_freshness import (
    ensure_repo_indexed_and_record as ensure_repo_indexed,
)
from backend.repo.repo_indexer import get_relevant_files
from backend.utils.json_helpers import clean_json_response

TRIAGE_TEMPERATURE = 0.2
TRIAGE_MAX_TOKENS = 4000

logger = logging.getLogger(__name__)

TRIAGE_SYSTEM_PROMPT = """You are a senior engineering lead.
Your job is to split a feature request into a small, safe chunk plan.

You must respond ONLY with a valid JSON object.
No markdown. No backticks. No explanation before or after.
Just the raw JSON object itself.

Chunking rules:
1. Each chunk must be independently testable.
2. Dependencies flow forward only.
3. DB migrations must always be isolated in their own chunk.
4. Security/auth/permissions/encryption must always be isolated and requires_human_review=true.
5. Each chunk must fit context window using token_estimate.
6. Split by engineering dependency order, not equal file count.
7. Do not create more chunks than needed.
8. For easy tasks, use 1 chunk.
9. For medium tasks, use 2-3 chunks.
10. For hard tasks, use 3-6 chunks.

files_expected grounding rules (CRITICAL — these paths are enforced as hard
scope by a later guard, so they must be precise, never guessed):
A. Use ONLY paths that appear in "RELEVANT INDEXED FILES", or a new file placed
   inside a directory that already contains an indexed file and uses the same
   file extension/language as that directory.
B. NEVER emit generic or invented paths such as src/models/user.py,
   src/api/auth.py, src/app.py, backend/src/..., frontend/src/components/X.js,
   or src/features/<feature_name>.py when those exact paths are not in the
   indexed files.
C. If you cannot determine the exact target files from the indexed files,
   return "files_expected": [] for that chunk, set risk_level="high",
   requires_human_review=true, and state in the rationale that the exact files
   could not be determined from the current repository index.
D. Prefer fewer, real files over many plausible-sounding ones. An empty
   files_expected is safer than an invented path.

The JSON must match this exact schema:
{
  "run_id": "<the run_id provided>",
  "project_id": "<the project_id provided>",
  "feature_description": "<the feature request>",
  "complexity": "easy",
  "total_chunks": 1,
  "reasoning": "why this chunk plan is appropriate",
  "chunks": [
    {
      "chunk_number": 1,
      "title": "short chunk title",
      "description": "what this chunk should do",
      "files_expected": ["relative/path.py"],
      "depends_on": [],
      "risk_level": "low",
      "token_estimate": 1000,
      "requires_human_review": false,
      "rationale": "why this chunk boundary exists"
    }
  ]
}

complexity must be one of: easy / medium / hard
risk_level must be one of: low / medium / high
High risk chunks must set requires_human_review=true.
Respond with nothing except the JSON object."""


def _format_relevant_files(relevant_files: list[dict]) -> str:
    if not relevant_files:
        return "No indexed files matched this feature request."

    lines = []
    for file_data in relevant_files:
        path = file_data.get("path", "")
        file_type = file_data.get("file_type", "unknown")
        tokens = file_data.get("token_estimate", 0)
        lines.append(f"- {path} | type={file_type} | tokens={tokens}")
    return "\n".join(lines)


def _build_triage_prompt(
    run_id: str,
    project_id: str,
    feature_description: str,
    relevant_files: list[dict],
    project_memory_block: str = "",
) -> str:
    memory_section = (
        f"\n\n{project_memory_block}\n\n"
        "Remember: Project memory is advisory. Current source code and "
        "explicit user instructions win on conflict."
        if project_memory_block
        else "\n\n"
    )
    return (
        f"RUN ID:\n{run_id}\n\n"
        f"PROJECT ID:\n{project_id}\n\n"
        f"FEATURE REQUEST:\n{feature_description}\n\n"
        f"RELEVANT INDEXED FILES:\n"
        f"{_format_relevant_files(relevant_files)}"
        f"{memory_section}"
        f"Apply these core rules exactly:\n"
        f"1. Each chunk must be independently testable.\n"
        f"2. Dependencies flow forward only.\n"
        f"3. DB migrations must always be isolated in their own chunk.\n"
        f"4. Security/auth/permissions/encryption must always be isolated "
        f"and requires_human_review=true.\n"
        f"5. Each chunk must fit context window using token_estimate.\n"
        f"6. files_expected must use ONLY paths from RELEVANT INDEXED FILES "
        f"above (or a new file inside a directory that already contains one of "
        f"those files, using the same extension). Do NOT invent generic paths "
        f"like src/..., backend/src/..., or frontend/src/...X.js. If the exact "
        f"files are unknown, use \"files_expected\": [], risk_level \"high\", "
        f"requires_human_review true, and say so in the rationale.\n\n"
        f"Respond with the JSON chunk plan only."
    )


def _build_llm_request(prompt: str) -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content=TRIAGE_SYSTEM_PROMPT),
            Message(role="user", content=prompt),
        ],
        model="",  # resolved per role by complete_for_role
        temperature=TRIAGE_TEMPERATURE,
        max_output_tokens=TRIAGE_MAX_TOKENS,
        response_format="json_object",
    )


async def _call_llm(prompt: str, run_id: str) -> str:
    response = await complete_for_role(
        Role.TRIAGE,
        _build_llm_request(prompt),
    )
    log_token_usage(response, run_id=run_id, role=Role.TRIAGE)
    return response.text


def _parse_triage(raw_text: str, run_id: str, project_id: str) -> TriageResult:
    cleaned = clean_json_response(raw_text)
    data = json.loads(cleaned)
    data["run_id"] = run_id
    data["project_id"] = project_id
    return TriageResult.model_validate(data)


async def _ensure_index_without_blocking(
    project_id: str,
    target_repo_path: str,
) -> None:
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            ensure_repo_indexed,
            project_id,
            target_repo_path,
        )
    except Exception as error:
        logger.warning(
            "[TRIAGE] Warning: repo indexing failed; continuing. "
            "project_id=%s | error=%s",
            project_id,
            error,
        )


async def run_triage(
    run_id: str,
    project_id: str,
    feature_description: str,
) -> TriageResult:
    """
    Build a validated chunk plan for a feature request.

    This function validates the project, attempts non-blocking repo indexing,
    asks Gemini for a JSON-only chunk plan, retries once on invalid output, and
    returns a TriageResult. It performs no database writes.
    """
    logger.info("[TRIAGE] Starting | run_id=%s | project_id=%s", run_id, project_id)

    try:
        project = require_project(project_id)
    except Exception as error:
        raise RuntimeError(
            f"triage.py: project validation failed. "
            f"project_id={project_id} | error={error}"
        )

    target_repo_path = project["repo_path"]
    await _ensure_index_without_blocking(project_id, target_repo_path)

    try:
        relevant_files = get_relevant_files(
            project_id,
            feature_description,
            limit=20,
        )
    except Exception as error:
        logger.warning(
            "[TRIAGE] Warning: relevant file lookup failed; continuing. "
            "project_id=%s | error=%s",
            project_id,
            error,
        )
        relevant_files = []

    memory_result = build_project_memory_block_detailed(
        project_id=project_id,
        role="triage",
        project_name=project.get("name"),
    )
    # Best-effort provenance capture from the SAME computation that built the
    # block. Never raises; failure here must not change the prompt or run.
    # Triage is run-level: chunk_number is None.
    capture_memory_injection(
        memory_result,
        run_id=run_id,
        project_id=project_id,
        role="triage",
        chunk_number=None,
    )

    prompt = _build_triage_prompt(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature_description,
        relevant_files=relevant_files,
        project_memory_block=memory_result.block,
    )

    raw_text = ""
    try:
        logger.info("[TRIAGE] Calling LLM (attempt 1)...")
        raw_text = await _call_llm(prompt, run_id)
        result = _parse_triage(raw_text, run_id, project_id)
        logger.info("[TRIAGE] Triage validated on attempt 1")
    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        logger.warning("[TRIAGE] Attempt 1 failed: %s", first_error)
        correction_prompt = (
            f"{prompt}\n\n"
            f"Your previous response was not valid JSON or did not match the "
            f"required schema.\n\n"
            f"Previous response was:\n{raw_text}\n\n"
            f"Error: {first_error}\n\n"
            f"Respond again with ONLY the raw JSON object."
        )

        try:
            logger.info("[TRIAGE] Calling LLM (attempt 2)...")
            raw_text = await _call_llm(correction_prompt, run_id)
            result = _parse_triage(raw_text, run_id, project_id)
            logger.info("[TRIAGE] Triage validated on attempt 2")
        except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
            raise RuntimeError(
                f"triage.py: LLM failed to return valid triage after "
                f"2 attempts. run_id={run_id} | error={second_error}"
            )
    except Exception as error:
        raise RuntimeError(
            f"triage.py: unexpected triage error. "
            f"run_id={run_id} | error={error}"
        )

    logger.info("[TRIAGE] Complete | run_id=%s", run_id)
    return result
