"""
report_analyzer.py
Read-only repository analyzer for report_only runs (PR #8A).

This module produces a useful, human-readable analysis report for read-only
("report_only") requests such as "review this repo", "find bugs", or "explain
this project". It is strictly read-only:

  - It NEVER calls the coder, patch applier, tester, local Git, PR orchestrator,
    GitHub client, or any chunked-execution mutation path.
  - It NEVER stages, commits, pushes, or creates pull requests.
  - It NEVER mutates source repository files. It only reads repo/index/context.
  - It may call an LLM analyzer for breadth/depth reasoning.

On any failure (project missing, indexing failure, LLM failure, malformed LLM
output) it degrades to a limited, safe read-only report. It never falls through
into triage, planning, or implementation.
"""

import asyncio
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text

from backend.db.database import engine
from backend.llm import complete_for_role, log_token_usage
from backend.llm.base import LLMRequest, Message
from backend.llm.role_config import Role
from backend.llm.sanitize import sanitize_for_log
from backend.projects.project_store import get_project
from backend.repo.repo_indexer import ensure_repo_indexed, get_relevant_files
from backend.utils.json_helpers import clean_json_response
from backend.utils.path_safety import validate_safe_relative_path

logger = logging.getLogger(__name__)

ANALYZER_TEMPERATURE = 0.2
ANALYZER_MAX_TOKENS = 4000

# Cost bounds. These keep a single read-only analysis cheap and predictable.
MAX_FILES_LISTED = 40
MAX_FILES_READ = 8
MAX_LINES_PER_FILE = 200
MAX_CHARS_PER_FILE = 8000

READ_ONLY_NOTE = (
    "Read-only report. No code was changed, no tests were run, "
    "no commits or PRs were created."
)

ANALYZER_SYSTEM_PROMPT = """You are a senior staff engineer performing a \
READ-ONLY code review. You may only read and reason about the provided \
repository context. You cannot and must not propose to run commands, you are \
not writing or applying any code, and no changes will be made as a result of \
your answer.

Respond ONLY with a valid JSON object. No markdown. No backticks. No text \
before or after. The JSON must match this exact schema:
{
  "summary": "a short paragraph answering the user's request",
  "findings": [
    {
      "title": "short finding title",
      "severity": "low | medium | high | info",
      "confidence": "low | medium | high",
      "file": "relative/path.py or null if general",
      "evidence": "what in the code/context supports this finding",
      "recommendation": "what a human could consider doing (advisory only)"
    }
  ],
  "limitations": ["what this read-only analysis could not verify"],
  "suggested_next_action": "one advisory sentence on a possible next step"
}

Rules:
- Ground every finding in the provided context. Do not invent files or APIs.
- If context is thin, say so honestly in limitations and keep findings modest.
- severity and confidence must be from the allowed values.
- Respond with nothing except the JSON object."""


class ReportAnalysisResult(BaseModel):
    """Public result of a read-only report analysis."""

    summary: str
    markdown_report: str
    files_reviewed: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class _Finding(BaseModel):
    title: str
    severity: str = "info"
    confidence: str = "unknown"
    file: str | None = None
    evidence: str = ""
    recommendation: str = ""


class _AnalyzerOutput(BaseModel):
    summary: str
    findings: list[_Finding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggested_next_action: str = ""


def _list_indexed_files(project_id: str, limit: int) -> list[dict]:
    """
    Return a breadth listing of indexed files for a project, used when keyword
    relevance matching finds nothing (e.g. "review this repo"). Read-only.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT path, file_type, token_estimate, line_count
                FROM file_index
                WHERE project_id = :project_id
                ORDER BY token_estimate DESC
            """), {"project_id": project_id}).fetchall()
        return [dict(row._mapping) for row in rows[:limit]]
    except Exception as error:
        logger.warning(
            "[REPORT] Breadth file listing failed; continuing. "
            "project_id=%s | error=%s",
            project_id,
            sanitize_for_log(str(error)),
        )
        return []


def _collect_candidate_files(
    project_id: str,
    feature_description: str,
) -> list[dict]:
    """
    Gather candidate files for the report: relevant (keyword-scored) files
    first, then a breadth listing fallback. Deduplicated by path. Read-only.
    """
    candidates: list[dict] = []
    seen: set[str] = set()

    try:
        relevant = get_relevant_files(
            project_id,
            feature_description,
            limit=MAX_FILES_LISTED,
        )
    except Exception as error:
        logger.warning(
            "[REPORT] Relevant file lookup failed; continuing. "
            "project_id=%s | error=%s",
            project_id,
            sanitize_for_log(str(error)),
        )
        relevant = []

    for row in relevant:
        path = row.get("path")
        if path and path not in seen:
            seen.add(path)
            candidates.append(row)

    if len(candidates) < MAX_FILES_LISTED:
        for row in _list_indexed_files(project_id, MAX_FILES_LISTED):
            path = row.get("path")
            if path and path not in seen:
                seen.add(path)
                candidates.append(row)
            if len(candidates) >= MAX_FILES_LISTED:
                break

    return candidates


def _read_bounded_file(repo_root: Path, relative_path: str) -> str | None:
    """
    Safely read a single repo file for depth context, enforcing path safety and
    truncating lines/characters. Returns None if the file cannot be read.
    Never writes.
    """
    try:
        full_path = validate_safe_relative_path(relative_path, repo_root)
        if not full_path.is_file():
            return None
        content = full_path.read_text(encoding="utf-8")
    except Exception as error:
        logger.warning(
            "[REPORT] Skipping unreadable file %s: %s",
            relative_path,
            sanitize_for_log(str(error)),
        )
        return None

    if "\x00" in content:
        return None

    lines = content.splitlines()
    truncated = False
    if len(lines) > MAX_LINES_PER_FILE:
        lines = lines[:MAX_LINES_PER_FILE]
        truncated = True
    text_block = "\n".join(lines)
    if len(text_block) > MAX_CHARS_PER_FILE:
        text_block = text_block[:MAX_CHARS_PER_FILE]
        truncated = True
    if truncated:
        text_block += "\n... [truncated for read-only analysis]"
    return text_block


def _read_files_for_depth(
    repo_path: str | None,
    candidates: list[dict],
) -> list[tuple[str, str]]:
    """
    Read a bounded number of real files for depth. Returns (path, content)
    pairs. Read-only; per-file failures are skipped.
    """
    if not repo_path:
        return []
    try:
        repo_root = Path(repo_path)
    except Exception:
        return []
    if not repo_root.exists() or not repo_root.is_dir():
        return []

    read_files: list[tuple[str, str]] = []
    for row in candidates:
        if len(read_files) >= MAX_FILES_READ:
            break
        path = row.get("path")
        if not path:
            continue
        content = _read_bounded_file(repo_root, path)
        if content is not None:
            read_files.append((path, content))
    return read_files


def _format_file_listing(candidates: list[dict]) -> str:
    if not candidates:
        return "No indexed files were available for this project."
    lines = []
    for row in candidates:
        path = row.get("path", "")
        file_type = row.get("file_type", "unknown")
        line_count = row.get("line_count", 0)
        lines.append(f"- {path} | type={file_type} | lines={line_count}")
    return "\n".join(lines)


def _format_file_excerpts(read_files: list[tuple[str, str]]) -> str:
    if not read_files:
        return "No file contents were read for depth in this analysis."
    blocks = []
    for path, content in read_files:
        blocks.append(f"### FILE: {path}\n{content}")
    return "\n\n".join(blocks)


def _build_user_prompt(
    feature_description: str,
    project: dict | None,
    candidates: list[dict],
    read_files: list[tuple[str, str]],
) -> str:
    project_name = "unknown"
    if project:
        project_name = project.get("name") or project.get("id") or "unknown"
    return (
        f"PROJECT: {project_name}\n\n"
        f"USER REQUEST (read-only):\n{feature_description}\n\n"
        f"INDEXED FILES (breadth):\n{_format_file_listing(candidates)}\n\n"
        f"FILE EXCERPTS (depth, truncated):\n"
        f"{_format_file_excerpts(read_files)}\n\n"
        f"Produce the read-only analysis JSON now. Ground findings in the "
        f"context above. Do not propose to run, write, or apply code."
    )


def _build_llm_request(prompt: str) -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content=ANALYZER_SYSTEM_PROMPT),
            Message(role="user", content=prompt),
        ],
        model="",
        temperature=ANALYZER_TEMPERATURE,
        max_output_tokens=ANALYZER_MAX_TOKENS,
        response_format="json_object",
    )


async def _call_llm(prompt: str, run_id: str) -> str:
    response = await complete_for_role(Role.SUMMARY, _build_llm_request(prompt))
    log_token_usage(response, run_id=run_id, role=Role.SUMMARY)
    return response.text


def _parse_analyzer_output(raw_text: str) -> _AnalyzerOutput:
    cleaned = clean_json_response(raw_text)
    data = json.loads(cleaned)
    return _AnalyzerOutput.model_validate(data)


async def _ensure_index_without_blocking(
    project_id: str,
    repo_path: str | None,
) -> None:
    if not repo_path:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            ensure_repo_indexed,
            project_id,
            repo_path,
        )
    except Exception as error:
        logger.warning(
            "[REPORT] Repo indexing failed; continuing read-only. "
            "project_id=%s | error=%s",
            project_id,
            sanitize_for_log(str(error)),
        )


def _build_markdown_report(
    feature_description: str,
    output: _AnalyzerOutput,
    files_reviewed: list[str],
    extra_limitations: list[str],
) -> str:
    lines: list[str] = []
    lines.append("# Read-only Analysis Report")
    lines.append("")
    lines.append("## Summary")
    lines.append(output.summary.strip() or "No summary was produced.")
    lines.append("")

    lines.append("## Findings")
    if output.findings:
        for index, finding in enumerate(output.findings, start=1):
            location = finding.file or "general"
            lines.append(f"### {index}. {finding.title}")
            lines.append(
                f"- Severity: {finding.severity} | "
                f"Confidence: {finding.confidence} | File: {location}"
            )
            if finding.evidence:
                lines.append(f"- Evidence / reasoning: {finding.evidence}")
            if finding.recommendation:
                lines.append(f"- Suggested consideration: {finding.recommendation}")
            lines.append("")
    else:
        lines.append("No specific findings were identified.")
        lines.append("")

    lines.append("## Files reviewed")
    if files_reviewed:
        for path in files_reviewed:
            lines.append(f"- {path}")
    else:
        lines.append("- No file contents were read for this analysis.")
    lines.append("")

    limitations = list(output.limitations) + list(extra_limitations)
    lines.append("## Limitations")
    if limitations:
        for limitation in limitations:
            lines.append(f"- {limitation}")
    else:
        lines.append("- None reported by the analyzer.")
    lines.append("")

    lines.append("## Suggested next action")
    lines.append(
        output.suggested_next_action.strip()
        or "Review the findings above and decide whether to request a plan."
    )
    lines.append("")

    lines.append("---")
    lines.append(READ_ONLY_NOTE)
    return "\n".join(lines)


def build_limited_report(feature_description: str, reason: str) -> str:
    """
    Build a safe, limited read-only report when full analysis is unavailable.
    Always read-only and always carries the no-mutation note.
    """
    safe_reason = sanitize_for_log(reason) if reason else "analysis unavailable"
    lines = [
        "# Read-only Analysis Report (limited)",
        "",
        "## Summary",
        "A full read-only analysis could not be completed for this request.",
        "",
        "## Request",
        feature_description.strip() or "(no request text)",
        "",
        "## Limitations",
        f"- The analyzer could not complete: {safe_reason}",
        "- No findings, severities, or file-level evidence are available.",
        "",
        "## Suggested next action",
        "Confirm the project and repository are indexed, then retry the "
        "read-only report.",
        "",
        "---",
        READ_ONLY_NOTE,
    ]
    return "\n".join(lines)


async def run_report_analysis(
    run_id: str,
    project_id: str,
    feature_description: str,
) -> ReportAnalysisResult:
    """
    Run a read-only analysis for a report_only request and return a readable
    report. This function never mutates the repository and never raises into an
    implementation path: any failure degrades to a limited, safe report.
    """
    logger.info(
        "[REPORT] Starting read-only analysis | run_id=%s | project_id=%s",
        run_id,
        project_id,
    )

    try:
        project = get_project(project_id)
    except Exception as error:
        logger.warning(
            "[REPORT] Project load failed; producing limited report. "
            "project_id=%s | error=%s",
            project_id,
            sanitize_for_log(str(error)),
        )
        project = None

    if project is None:
        report = build_limited_report(
            feature_description,
            "project not found or could not be loaded",
        )
        return ReportAnalysisResult(
            summary="Project not found; limited read-only report produced.",
            markdown_report=report,
            files_reviewed=[],
            limitations=["Project could not be loaded."],
        )

    repo_path = project.get("repo_path")
    await _ensure_index_without_blocking(project_id, repo_path)

    candidates = _collect_candidate_files(project_id, feature_description)
    read_files = _read_files_for_depth(repo_path, candidates)
    files_reviewed = [path for path, _ in read_files]

    extra_limitations: list[str] = []
    if not candidates:
        extra_limitations.append(
            "No repository index was available; breadth was limited."
        )
    if not read_files:
        extra_limitations.append(
            "No file contents could be read; analysis relied on metadata only."
        )

    prompt = _build_user_prompt(
        feature_description=feature_description,
        project=project,
        candidates=candidates,
        read_files=read_files,
    )

    raw_text = ""
    output: _AnalyzerOutput | None = None
    try:
        raw_text = await _call_llm(prompt, run_id)
        output = _parse_analyzer_output(raw_text)
    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        logger.warning(
            "[REPORT] Analyzer output invalid; retrying once. run_id=%s | error=%s",
            run_id,
            sanitize_for_log(str(first_error)),
        )
        correction_prompt = (
            f"{prompt}\n\n"
            f"Your previous response was not valid JSON or did not match the "
            f"required schema.\n\nPrevious response was:\n{raw_text}\n\n"
            f"Error: {sanitize_for_log(str(first_error))}\n\n"
            f"Respond again with ONLY the raw JSON object."
        )
        try:
            raw_text = await _call_llm(correction_prompt, run_id)
            output = _parse_analyzer_output(raw_text)
        except Exception as second_error:
            logger.warning(
                "[REPORT] Analyzer failed after retry; producing limited "
                "report. run_id=%s | error=%s",
                run_id,
                sanitize_for_log(str(second_error)),
            )
            output = None
    except Exception as error:
        logger.warning(
            "[REPORT] Analyzer LLM call failed; producing limited report. "
            "run_id=%s | error=%s",
            run_id,
            sanitize_for_log(str(error)),
        )
        output = None

    if output is None:
        report = build_limited_report(
            feature_description,
            "the analyzer did not return a usable result",
        )
        limitations = ["The analyzer did not return a usable result."]
        limitations.extend(extra_limitations)
        return ReportAnalysisResult(
            summary="Limited read-only report produced after analyzer failure.",
            markdown_report=report,
            files_reviewed=files_reviewed,
            limitations=limitations,
        )

    markdown_report = _build_markdown_report(
        feature_description=feature_description,
        output=output,
        files_reviewed=files_reviewed,
        extra_limitations=extra_limitations,
    )
    logger.info(
        "[REPORT] Analysis complete | run_id=%s | files_reviewed=%s | findings=%s",
        run_id,
        len(files_reviewed),
        len(output.findings),
    )
    return ReportAnalysisResult(
        summary=output.summary,
        markdown_report=markdown_report,
        files_reviewed=files_reviewed,
        limitations=list(output.limitations) + extra_limitations,
    )
