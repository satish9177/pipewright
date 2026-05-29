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
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator
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

# Shared output contract appended to every kind-specific prompt. The schema is
# identical across report kinds (PR #8B ReportResult), only the analytic focus
# changes.
_SCHEMA_AND_RULES = """Respond ONLY with a valid JSON object. No markdown. No \
backticks. No text before or after. The JSON must match this exact schema:
{
  "summary": "a short paragraph answering the user's request",
  "findings": [
    {
      "title": "short finding title",
      "severity": "info | low | medium | high | critical",
      "confidence": "low | medium | high",
      "file": "relative/path.py or null if general",
      "line": "line number or range hint, or null",
      "evidence": "what in the code/context supports this finding",
      "reasoning": "why this matters (advisory)",
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


# project_explanation: explain how the system works; informational sections,
# not a bug audit.
_PROJECT_EXPLANATION_GUIDANCE = """You are a senior staff engineer writing a \
READ-ONLY explanation of a codebase for someone who wants to understand how it \
works. You may only read and reason about the provided repository context. You \
are NOT reviewing for bugs and you are not writing or applying any code.

Your job is to EXPLAIN the project, not to audit it. The "summary" should give \
a clear overview of what the project does and how it is structured. Each \
"finding" is an INFORMATIONAL SECTION describing one aspect of the system. Use \
sections such as (only those the context actually supports):
- Project overview
- Backend architecture / modules
- Frontend architecture / modules
- Execution / data flow
- Storage / database
- Integrations / external tools
- Tests / dev workflow
- Important files / modules

For these informational findings:
- severity MUST be "info".
- confidence should reflect how strongly the context supports the explanation.
- Put the explanation in "evidence" and "reasoning"; cite real files in "file".
- "recommendation" is learning/exploration oriented (e.g. "read X to go \
deeper"), never "fix this".
Only mention a bug or risk if it is obvious and important; do not turn this \
into a code audit.
"suggested_next_action" should help the reader learn more about the system, \
not fix it."""


# issue_review: hunt for grounded bugs/risks with severity and confidence.
_ISSUE_REVIEW_GUIDANCE = """You are a senior staff engineer performing a \
READ-ONLY code review looking for problems. You may only read and reason about \
the provided repository context. You are not writing or applying any code.

Find concrete bugs, security risks, validation gaps, error-handling problems, \
missing tests, and risky edge cases. For each "finding":
- Ground it in specific evidence from the context; cite the real "file".
- Set "severity" (info|low|medium|high|critical) and "confidence" \
(low|medium|high) honestly.
- "recommendation" is an advisory suggested fix (no code, no commands).
Avoid generic or speculative "maybe" findings with no supporting evidence. If \
you cannot find well-grounded issues, return few or no findings and explain \
why in limitations rather than inventing problems.
"suggested_next_action" should point at the most important issue to look \
into."""


# feature_discovery: grounded ideas for what to build/improve next.
_FEATURE_DISCOVERY_GUIDANCE = """You are a product-minded senior staff \
engineer suggesting READ-ONLY ideas for what could be built or improved next. \
You may only read and reason about the provided repository context. You are \
not writing or applying any code.

Suggest feature ideas and improvements that are GROUNDED in what the \
repository already is. Each "finding" is one idea. For each:
- "title" names the idea (e.g. "Add X", "Enhance Y", "Improve Z").
- Use severity "info" or "low"; confidence reflects how well the context \
supports the idea.
- In "evidence"/"reasoning", explain the user/business value, the likely area \
or files involved (cite real files where visible), and the difficulty/risk.
- "recommendation" describes a sensible first step (advisory only).
Do NOT suggest a feature as new if the context shows it already exists. If a \
capability is partially present, frame it as "enhance/complete/improve", not \
"add from scratch".
If the project is tiny or the context is minimal, say so in limitations and \
note that suggestions are based on limited context.
Mark the single best first idea in "suggested_next_action"."""


# general_analysis: the original balanced read-only review (PR #8A/#8B).
_GENERAL_ANALYSIS_GUIDANCE = """You are a senior staff engineer performing a \
READ-ONLY analysis. You may only read and reason about the provided \
repository context. You cannot and must not propose to run commands, you are \
not writing or applying any code, and no changes will be made as a result of \
your answer. Answer the user's request directly and ground every finding in \
the provided context."""


# Allowed structured values. Anything the LLM returns outside these sets is
# normalized to a safe default rather than crashing the read-only report.
SEVERITY_VALUES = {"info", "low", "medium", "high", "critical"}
CONFIDENCE_VALUES = {"low", "medium", "high"}
DEFAULT_SEVERITY = "info"
DEFAULT_CONFIDENCE = "low"

class ReportKind(str, Enum):
    """
    Read-only report specialization (PR #8C). The value is what gets stored in
    ReportResult.report_kind / report_json and read by ReportView.
    """

    PROJECT_EXPLANATION = "project_explanation"
    ISSUE_REVIEW = "issue_review"
    FEATURE_DISCOVERY = "feature_discovery"
    GENERAL_ANALYSIS = "general_analysis"


# Deterministic intent hints. Order of evaluation is fixed in
# classify_report_kind below so the user's clear intent always wins.
_ISSUE_REVIEW_HINTS = (
    "issue",
    "bug",
    "security",
    "vulnerab",
    "risk",
    "problem",
    "broken",
    "what is broken",
    "whats broken",
    "what's broken",
    "what is wrong",
    "what's wrong",
    "whats wrong",
    "review code",
    "review the code",
    "code review",
    "review for",
    "error handling",
    "identify problem",
    "find flaws",
)

_FEATURE_DISCOVERY_HINTS = (
    "feature",
    "suggest",
    "improve",
    "improvement",
    "enhance",
    "build next",
    "what should we build",
    "can we add",
    "what to add",
    "next feature",
)

_PROJECT_EXPLANATION_HINTS = (
    "explain",
    "architecture",
    "how does this",
    "how does it",
    "how it works",
    "how this works",
    "how the project works",
    "how the app works",
    "summarize",
    "summarise",
    "walk me through",
    "walk through",
    "what does this project do",
    "what does this do",
    "what does it do",
    "overview",
    "understand the codebase",
    "understand this project",
    "describe this project",
    "describe the project",
)


def classify_report_kind(feature_description: str) -> ReportKind:
    """
    Deterministically classify a read-only report request into a ReportKind.

    Rules are checked in priority order so a clear user intent wins for the
    common cases: an explicit ask for issues/bugs (issue_review) or for feature
    ideas (feature_discovery) takes precedence over a generic "explain" verb,
    and an explanation request maps to project_explanation. Anything that does
    not clearly match falls back to general_analysis. This is intentionally
    rule-first; no LLM call is made to decide the kind.
    """
    text_value = (feature_description or "").lower()

    if any(hint in text_value for hint in _ISSUE_REVIEW_HINTS):
        return ReportKind.ISSUE_REVIEW
    if any(hint in text_value for hint in _FEATURE_DISCOVERY_HINTS):
        return ReportKind.FEATURE_DISCOVERY
    if any(hint in text_value for hint in _PROJECT_EXPLANATION_HINTS):
        return ReportKind.PROJECT_EXPLANATION
    return ReportKind.GENERAL_ANALYSIS


_KIND_GUIDANCE: dict[ReportKind, str] = {
    ReportKind.PROJECT_EXPLANATION: _PROJECT_EXPLANATION_GUIDANCE,
    ReportKind.ISSUE_REVIEW: _ISSUE_REVIEW_GUIDANCE,
    ReportKind.FEATURE_DISCOVERY: _FEATURE_DISCOVERY_GUIDANCE,
    ReportKind.GENERAL_ANALYSIS: _GENERAL_ANALYSIS_GUIDANCE,
}


def _build_system_prompt(kind: ReportKind) -> str:
    """Compose the kind-specific guidance with the shared output contract."""
    guidance = _KIND_GUIDANCE.get(kind, _GENERAL_ANALYSIS_GUIDANCE)
    return f"{guidance}\n\n{_SCHEMA_AND_RULES}"


def _normalize_severity(value: str | None) -> str:
    if value and value.strip().lower() in SEVERITY_VALUES:
        return value.strip().lower()
    return DEFAULT_SEVERITY


def _normalize_confidence(value: str | None) -> str:
    if value and value.strip().lower() in CONFIDENCE_VALUES:
        return value.strip().lower()
    return DEFAULT_CONFIDENCE


class ReportFinding(BaseModel):
    """One structured, read-only finding rendered in ReportView."""

    title: str
    severity: str = DEFAULT_SEVERITY
    confidence: str = DEFAULT_CONFIDENCE
    file_path: str | None = None
    line_hint: str | None = None
    evidence: str = ""
    reasoning: str = ""
    suggested_next_action: str = ""


class ReportResult(BaseModel):
    """
    Structured source of truth for a read-only report. Stored as report_json
    on the run and rendered by ReportView. Intentionally permissive so a thin
    or partial analysis still serializes cleanly.
    """

    summary: str = ""
    findings: list[ReportFinding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    files_reviewed: list[str] = Field(default_factory=list)
    implementation_recommended: bool = False
    next_action: str = ""
    report_kind: str | None = None


class ReportAnalysisResult(BaseModel):
    """Public result of a read-only report analysis."""

    summary: str
    markdown_report: str
    files_reviewed: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    # Structured form for ReportView. None when only a limited/fallback report
    # could be produced; callers then persist plain_english_summary only.
    report_result: ReportResult | None = None


class _Finding(BaseModel):
    title: str
    severity: str = "info"
    confidence: str = "unknown"
    file: str | None = None
    line: str | None = None
    evidence: str = ""
    reasoning: str = ""
    recommendation: str = ""

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value):
        if value is None:
            return None
        return str(value)


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


def _build_llm_request(prompt: str, kind: ReportKind) -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content=_build_system_prompt(kind)),
            Message(role="user", content=prompt),
        ],
        model="",
        temperature=ANALYZER_TEMPERATURE,
        max_output_tokens=ANALYZER_MAX_TOKENS,
        response_format="json_object",
    )


async def _call_llm(prompt: str, run_id: str, kind: ReportKind) -> str:
    response = await complete_for_role(
        Role.SUMMARY, _build_llm_request(prompt, kind)
    )
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


def _build_report_result(
    report_kind: ReportKind,
    output: _AnalyzerOutput,
    files_reviewed: list[str],
    extra_limitations: list[str],
) -> ReportResult:
    """
    Map the validated analyzer output into the structured ReportResult that is
    persisted as report_json and rendered by ReportView. Severity/confidence
    are normalized so an out-of-range LLM value never crashes the report.
    """
    findings: list[ReportFinding] = []
    for finding in output.findings:
        findings.append(ReportFinding(
            title=finding.title,
            severity=_normalize_severity(finding.severity),
            confidence=_normalize_confidence(finding.confidence),
            file_path=finding.file or None,
            line_hint=finding.line or None,
            evidence=finding.evidence,
            reasoning=finding.reasoning,
            suggested_next_action=finding.recommendation,
        ))
    return ReportResult(
        summary=output.summary,
        findings=findings,
        limitations=list(output.limitations) + list(extra_limitations),
        files_reviewed=files_reviewed,
        # Read-only report: implementation is never auto-recommended. The
        # next_action below is advisory only; there is no handoff in this PR.
        implementation_recommended=False,
        next_action=output.suggested_next_action,
        report_kind=report_kind.value,
    )


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

    # Deterministic, rule-first specialization (PR #8C). Drives both the
    # analyzer prompt focus and the stored report_kind.
    report_kind = classify_report_kind(feature_description)
    logger.info(
        "[REPORT] Report kind classified | run_id=%s | report_kind=%s",
        run_id,
        report_kind.value,
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
        raw_text = await _call_llm(prompt, run_id, report_kind)
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
            raw_text = await _call_llm(correction_prompt, run_id, report_kind)
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
    report_result = _build_report_result(
        report_kind=report_kind,
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
        report_result=report_result,
    )
