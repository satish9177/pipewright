"""
triage.py
Phase 2B chunk planning stage.

Triage turns one feature request into an ordered chunk plan. It does not create
pipeline_runs rows and it never executes chunks.
"""

import asyncio
import json

import google.generativeai as genai
from pydantic import ValidationError

from backend.config.keys import settings
from backend.models.chunk import TriageResult
from backend.projects.project_store import require_project
from backend.repo.repo_indexer import ensure_repo_indexed, get_relevant_files
from backend.utils.json_helpers import clean_json_response

TRIAGE_MODEL = "gemini-2.5-flash-lite"
TRIAGE_TEMPERATURE = 0.2
TRIAGE_MAX_TOKENS = 4000

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
) -> str:
    return (
        f"RUN ID:\n{run_id}\n\n"
        f"PROJECT ID:\n{project_id}\n\n"
        f"FEATURE REQUEST:\n{feature_description}\n\n"
        f"RELEVANT INDEXED FILES:\n"
        f"{_format_relevant_files(relevant_files)}\n\n"
        f"Apply these core rules exactly:\n"
        f"1. Each chunk must be independently testable.\n"
        f"2. Dependencies flow forward only.\n"
        f"3. DB migrations must always be isolated in their own chunk.\n"
        f"4. Security/auth/permissions/encryption must always be isolated "
        f"and requires_human_review=true.\n"
        f"5. Each chunk must fit context window using token_estimate.\n\n"
        f"Respond with the JSON chunk plan only."
    )


def _log_token_usage(response, run_id: str) -> None:
    try:
        usage = response.usage_metadata
        print(
            f"[TRIAGE] Token usage | "
            f"run_id={run_id} | "
            f"model={TRIAGE_MODEL} | "
            f"input={usage.prompt_token_count} | "
            f"output={usage.candidates_token_count}"
        )
    except Exception:
        print(f"[TRIAGE] Token usage unavailable | run_id={run_id}")


def _call_gemini(prompt: str, run_id: str) -> str:
    genai.configure(api_key=settings.gemini_api_key)
    generation_config = genai.GenerationConfig(
        temperature=TRIAGE_TEMPERATURE,
        max_output_tokens=TRIAGE_MAX_TOKENS,
    )
    model = genai.GenerativeModel(
        model_name=TRIAGE_MODEL,
        generation_config=generation_config,
        system_instruction=TRIAGE_SYSTEM_PROMPT,
    )
    response = model.generate_content(prompt)
    _log_token_usage(response, run_id)
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
        print(
            f"[TRIAGE] Warning: repo indexing failed; continuing. "
            f"project_id={project_id} | error={error}"
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
    print(f"[TRIAGE] Starting | run_id={run_id} | project_id={project_id}")

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
        print(
            f"[TRIAGE] Warning: relevant file lookup failed; continuing. "
            f"project_id={project_id} | error={error}"
        )
        relevant_files = []

    prompt = _build_triage_prompt(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature_description,
        relevant_files=relevant_files,
    )

    raw_text = ""
    try:
        print("[TRIAGE] Calling Gemini (attempt 1)...")
        raw_text = _call_gemini(prompt, run_id)
        result = _parse_triage(raw_text, run_id, project_id)
        print("[TRIAGE] Triage validated on attempt 1")
    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        print(f"[TRIAGE] Attempt 1 failed: {first_error}")
        correction_prompt = (
            f"{prompt}\n\n"
            f"Your previous response was not valid JSON or did not match the "
            f"required schema.\n\n"
            f"Previous response was:\n{raw_text}\n\n"
            f"Error: {first_error}\n\n"
            f"Respond again with ONLY the raw JSON object."
        )

        try:
            print("[TRIAGE] Calling Gemini (attempt 2)...")
            raw_text = _call_gemini(correction_prompt, run_id)
            result = _parse_triage(raw_text, run_id, project_id)
            print("[TRIAGE] Triage validated on attempt 2")
        except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
            raise RuntimeError(
                f"triage.py: Gemini failed to return valid triage after "
                f"2 attempts. run_id={run_id} | error={second_error}"
            )
    except Exception as error:
        raise RuntimeError(
            f"triage.py: unexpected triage error. "
            f"run_id={run_id} | error={error}"
        )

    print(f"[TRIAGE] Complete | run_id={run_id}")
    return result
