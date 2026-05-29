"""
test_report_analyzer.py
Tests for the PR #8A read-only report analyzer.

No real API calls: the analyzer LLM is mocked. These tests prove the analyzer
produces a useful read-only report, degrades safely on failure, enforces path
safety / bounds when reading files, and never imports or touches any mutation
module or implementation path.
"""

import json
import uuid
from pathlib import Path

import pytest

from backend.llm.base import LLMResponse
from backend.llm.role_config import Role
from backend.pipeline import report_analyzer
from backend.pipeline.report_analyzer import (
    READ_ONLY_NOTE,
    ReportAnalysisResult,
    ReportKind,
    ReportResult,
    _build_system_prompt,
    build_limited_report,
    classify_report_kind,
    run_report_analysis,
)

pytestmark = pytest.mark.unit


VALID_ANALYZER_JSON = json.dumps({
    "summary": "Small FastAPI service that orchestrates chunked runs.",
    "findings": [
        {
            "title": "Broad exception handling",
            "severity": "medium",
            "confidence": "high",
            "file": "backend/app.py",
            "evidence": "Bare except blocks swallow errors.",
            "recommendation": "Narrow the caught exception types.",
        }
    ],
    "limitations": ["Only a sample of files was reviewed."],
    "suggested_next_action": "Request a plan for the medium-severity finding.",
})


def _patch_dependencies(
    monkeypatch,
    tmp_repo,
    *,
    relevant_files=None,
    indexed_files=None,
):
    monkeypatch.setattr(
        report_analyzer,
        "get_project",
        lambda project_id: {
            "id": project_id,
            "name": "Test Project",
            "repo_path": str(tmp_repo),
        },
    )
    monkeypatch.setattr(
        report_analyzer,
        "ensure_repo_indexed",
        lambda project_id, repo_path: {"status": "already_indexed"},
    )
    monkeypatch.setattr(
        report_analyzer,
        "get_relevant_files",
        lambda project_id, query, limit=20: list(relevant_files or []),
    )
    monkeypatch.setattr(
        report_analyzer,
        "_list_indexed_files",
        lambda project_id, limit: list(indexed_files or []),
    )


def _patch_llm(monkeypatch, payloads):
    """Patch the analyzer LLM to return the given payload(s) in sequence."""
    if isinstance(payloads, str):
        payloads = [payloads]
    calls = {"count": 0, "roles": []}

    async def fake_complete_for_role(role, request):
        calls["roles"].append(role)
        index = min(calls["count"], len(payloads) - 1)
        text_payload = payloads[index]
        calls["count"] += 1
        return LLMResponse(
            text=text_payload,
            provider="fake",
            model=request.model or "fake-model",
            input_tokens=10,
            output_tokens=5,
            finish_reason="stop",
        )

    monkeypatch.setattr(report_analyzer, "complete_for_role", fake_complete_for_role)
    return calls


@pytest.mark.asyncio
async def test_produces_useful_non_canned_report(monkeypatch, tmp_repo):
    (tmp_repo / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _patch_dependencies(
        monkeypatch,
        tmp_repo,
        relevant_files=[{
            "path": "app.py",
            "file_type": "unknown",
            "line_count": 2,
        }],
    )
    _patch_llm(monkeypatch, VALID_ANALYZER_JSON)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert isinstance(result, ReportAnalysisResult)
    report = result.markdown_report
    assert "Broad exception handling" in report
    assert "## Summary" in report
    assert "## Findings" in report
    assert "## Files reviewed" in report
    assert "## Limitations" in report
    assert "## Suggested next action" in report
    # Severity and confidence surfaced where available.
    assert "Severity: medium" in report
    assert "Confidence: high" in report
    assert "app.py" in result.files_reviewed


@pytest.mark.asyncio
async def test_report_includes_read_only_messaging(monkeypatch, tmp_repo):
    _patch_dependencies(monkeypatch, tmp_repo)
    _patch_llm(monkeypatch, VALID_ANALYZER_JSON)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "explain repo")

    assert READ_ONLY_NOTE in result.markdown_report
    assert (
        "No code was changed, no tests were run, no commits or PRs were created"
        in result.markdown_report
    )


@pytest.mark.asyncio
async def test_analyzer_uses_read_only_summary_role(monkeypatch, tmp_repo):
    _patch_dependencies(monkeypatch, tmp_repo)
    calls = _patch_llm(monkeypatch, VALID_ANALYZER_JSON)

    await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert calls["roles"]
    # Read-only analysis must not use a coder/implementation role.
    assert all(role == Role.SUMMARY for role in calls["roles"])
    assert Role.CODER not in calls["roles"]


@pytest.mark.asyncio
async def test_malformed_output_retries_then_succeeds(monkeypatch, tmp_repo):
    _patch_dependencies(monkeypatch, tmp_repo)
    calls = _patch_llm(monkeypatch, ["not json", VALID_ANALYZER_JSON])

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert calls["count"] == 2
    assert "Broad exception handling" in result.markdown_report


@pytest.mark.asyncio
async def test_malformed_output_twice_degrades_to_limited_report(monkeypatch, tmp_repo):
    _patch_dependencies(monkeypatch, tmp_repo)
    _patch_llm(monkeypatch, ["garbage", "still garbage"])

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    # Fails safely as a limited read-only report; never raises.
    assert "limited" in result.markdown_report.lower()
    assert READ_ONLY_NOTE in result.markdown_report


@pytest.mark.asyncio
async def test_llm_exception_degrades_to_limited_report(monkeypatch, tmp_repo):
    _patch_dependencies(monkeypatch, tmp_repo)

    async def boom(role, request):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(report_analyzer, "complete_for_role", boom)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert "limited" in result.markdown_report.lower()
    assert READ_ONLY_NOTE in result.markdown_report


@pytest.mark.asyncio
async def test_missing_project_returns_safe_limited_report(monkeypatch, tmp_repo):
    monkeypatch.setattr(report_analyzer, "get_project", lambda project_id: None)

    async def should_not_call(role, request):
        raise AssertionError("LLM should not be called when project is missing")

    monkeypatch.setattr(report_analyzer, "complete_for_role", should_not_call)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-missing", "review")

    assert "limited" in result.markdown_report.lower()
    assert READ_ONLY_NOTE in result.markdown_report
    assert result.files_reviewed == []


@pytest.mark.asyncio
async def test_no_index_or_files_still_returns_report(monkeypatch, tmp_repo):
    # No relevant files and no breadth listing available.
    _patch_dependencies(monkeypatch, tmp_repo, relevant_files=[], indexed_files=[])
    _patch_llm(monkeypatch, VALID_ANALYZER_JSON)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert result.files_reviewed == []
    assert READ_ONLY_NOTE in result.markdown_report
    # Limitation about missing index/files is surfaced.
    assert any("index" in lim.lower() or "metadata" in lim.lower()
               for lim in result.limitations)


@pytest.mark.asyncio
async def test_path_traversal_candidate_is_not_read(monkeypatch, tmp_repo):
    # A secret outside the repo that must never be read.
    outside = tmp_repo.parent / "outside_secret.txt"
    outside.write_text("TOP SECRET", encoding="utf-8")
    (tmp_repo / "safe.py").write_text("x = 1\n", encoding="utf-8")

    captured = {"prompt": ""}

    async def capture(role, request):
        captured["prompt"] = request.messages[1].content
        return LLMResponse(
            text=VALID_ANALYZER_JSON,
            provider="fake",
            model="fake-model",
        )

    _patch_dependencies(
        monkeypatch,
        tmp_repo,
        relevant_files=[
            {"path": "../outside_secret.txt", "file_type": "unknown", "line_count": 1},
            {"path": "safe.py", "file_type": "unknown", "line_count": 1},
        ],
    )
    monkeypatch.setattr(report_analyzer, "complete_for_role", capture)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert "../outside_secret.txt" not in result.files_reviewed
    assert "TOP SECRET" not in captured["prompt"]
    assert "safe.py" in result.files_reviewed


@pytest.mark.asyncio
async def test_file_contents_are_truncated(monkeypatch, tmp_repo):
    big_lines = "\n".join(f"line {i}" for i in range(1000))
    (tmp_repo / "big.py").write_text(big_lines, encoding="utf-8")

    captured = {"prompt": ""}

    async def capture(role, request):
        captured["prompt"] = request.messages[1].content
        return LLMResponse(text=VALID_ANALYZER_JSON, provider="fake", model="m")

    _patch_dependencies(
        monkeypatch,
        tmp_repo,
        relevant_files=[{"path": "big.py", "file_type": "unknown", "line_count": 1000}],
    )
    monkeypatch.setattr(report_analyzer, "complete_for_role", capture)

    await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert "truncated for read-only analysis" in captured["prompt"]
    assert "line 999" not in captured["prompt"]


@pytest.mark.asyncio
async def test_returns_structured_report_result(monkeypatch, tmp_repo):
    (tmp_repo / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _patch_dependencies(
        monkeypatch,
        tmp_repo,
        relevant_files=[{"path": "app.py", "file_type": "unknown", "line_count": 2}],
    )
    _patch_llm(monkeypatch, VALID_ANALYZER_JSON)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert isinstance(result.report_result, ReportResult)
    report = result.report_result
    assert report.summary
    assert report.files_reviewed == ["app.py"]
    assert report.limitations  # analyzer + extra limitations merged
    assert report.next_action
    # "review repo" matches no specialized intent -> general_analysis fallback.
    assert report.report_kind == "general_analysis"
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.title == "Broad exception handling"
    assert finding.severity == "medium"
    assert finding.confidence == "high"
    assert finding.file_path == "backend/app.py"
    assert finding.evidence
    assert finding.suggested_next_action
    # Read-only report never auto-recommends implementation.
    assert report.implementation_recommended is False


@pytest.mark.asyncio
async def test_unknown_severity_confidence_normalized_to_defaults(
    monkeypatch, tmp_repo
):
    payload = json.dumps({
        "summary": "Summary.",
        "findings": [{
            "title": "Odd finding",
            "severity": "catastrophic",   # not an allowed value
            "confidence": "certain",      # not an allowed value
            "file": "app.py",
            "line": 42,                   # int -> coerced to str
            "evidence": "evidence",
            "reasoning": "reasoning",
            "recommendation": "consider it",
        }],
        "limitations": [],
        "suggested_next_action": "look closer",
    })
    _patch_dependencies(monkeypatch, tmp_repo)
    _patch_llm(monkeypatch, payload)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    finding = result.report_result.findings[0]
    # Unknown values normalized to safe defaults rather than crashing.
    assert finding.severity == "info"
    assert finding.confidence == "low"
    assert finding.line_hint == "42"
    assert finding.reasoning == "reasoning"


@pytest.mark.asyncio
async def test_empty_findings_still_structured(monkeypatch, tmp_repo):
    payload = json.dumps({
        "summary": "Nothing notable.",
        "findings": [],
        "limitations": ["thin context"],
        "suggested_next_action": "no action needed",
    })
    _patch_dependencies(monkeypatch, tmp_repo)
    _patch_llm(monkeypatch, payload)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    assert isinstance(result.report_result, ReportResult)
    assert result.report_result.findings == []
    assert "thin context" in result.report_result.limitations


@pytest.mark.asyncio
async def test_discovery_request_sets_report_kind_feature_discovery(
    monkeypatch, tmp_repo
):
    _patch_dependencies(monkeypatch, tmp_repo)
    _patch_llm(monkeypatch, VALID_ANALYZER_JSON)

    result = await run_report_analysis(
        str(uuid.uuid4()), "proj-x", "what features can we add to this project"
    )

    assert result.report_result is not None
    assert result.report_result.report_kind == "feature_discovery"


@pytest.mark.asyncio
async def test_limited_report_has_no_structured_result(monkeypatch, tmp_repo):
    # Analyzer returns garbage on every attempt -> limited fallback report.
    _patch_dependencies(monkeypatch, tmp_repo)
    _patch_llm(monkeypatch, ["garbage", "still garbage"])

    result = await run_report_analysis(str(uuid.uuid4()), "proj-x", "review repo")

    # No structured result: callers fall back to plain_english_summary.
    assert result.report_result is None
    assert "limited" in result.markdown_report.lower()


@pytest.mark.asyncio
async def test_missing_project_has_no_structured_result(monkeypatch, tmp_repo):
    monkeypatch.setattr(report_analyzer, "get_project", lambda project_id: None)

    result = await run_report_analysis(str(uuid.uuid4()), "proj-missing", "review")

    assert result.report_result is None


# ---------------------------------------------------------------------------
# PR #8C: report intent specialization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("feature_text,expected", [
    # project_explanation
    ("explain this project", ReportKind.PROJECT_EXPLANATION),
    ("what does this project do", ReportKind.PROJECT_EXPLANATION),
    ("explain the architecture", ReportKind.PROJECT_EXPLANATION),
    ("how does this project work", ReportKind.PROJECT_EXPLANATION),
    ("summarize this repo", ReportKind.PROJECT_EXPLANATION),
    ("walk me through the codebase", ReportKind.PROJECT_EXPLANATION),
    # issue_review
    ("is there any issue in the code", ReportKind.ISSUE_REVIEW),
    ("find bugs", ReportKind.ISSUE_REVIEW),
    ("review for bugs", ReportKind.ISSUE_REVIEW),
    ("any security issues?", ReportKind.ISSUE_REVIEW),
    ("what is broken", ReportKind.ISSUE_REVIEW),
    ("identify problems", ReportKind.ISSUE_REVIEW),
    # feature_discovery
    ("what features can we add", ReportKind.FEATURE_DISCOVERY),
    ("what feature kind can we add", ReportKind.FEATURE_DISCOVERY),
    ("suggest features", ReportKind.FEATURE_DISCOVERY),
    ("suggest improvements for this project", ReportKind.FEATURE_DISCOVERY),
    ("what can we improve", ReportKind.FEATURE_DISCOVERY),
    ("what should we build next", ReportKind.FEATURE_DISCOVERY),
    ("how can we improve this app", ReportKind.FEATURE_DISCOVERY),
    # general_analysis fallback
    ("tell me about widgets", ReportKind.GENERAL_ANALYSIS),
    ("review repo", ReportKind.GENERAL_ANALYSIS),
    ("", ReportKind.GENERAL_ANALYSIS),
])
def test_classify_report_kind(feature_text, expected):
    assert classify_report_kind(feature_text) == expected


def test_classify_report_kind_issue_wins_over_explain():
    # A clear ask for issues beats a generic "explain" verb.
    assert classify_report_kind("explain any bugs") == ReportKind.ISSUE_REVIEW


def test_project_explanation_prompt_focuses_on_architecture_not_audit():
    prompt = _build_system_prompt(ReportKind.PROJECT_EXPLANATION).lower()
    assert "architecture" in prompt
    assert "execution" in prompt or "data flow" in prompt
    assert "explain" in prompt
    # Informational, not a code audit.
    assert "do not turn this into a code audit" in prompt
    assert "severity must be" in prompt or 'severity must be "info"' in prompt


def test_issue_review_prompt_focuses_on_bugs_and_risks():
    prompt = _build_system_prompt(ReportKind.ISSUE_REVIEW).lower()
    assert "bug" in prompt
    assert "security" in prompt
    assert "risk" in prompt or "edge case" in prompt
    assert "evidence" in prompt


def test_feature_discovery_prompt_focuses_on_ideas_value_difficulty():
    prompt = _build_system_prompt(ReportKind.FEATURE_DISCOVERY).lower()
    assert "feature" in prompt
    assert "value" in prompt
    assert "difficulty" in prompt or "risk" in prompt
    # Must avoid presenting already-implemented features as new.
    assert "already exist" in prompt


def test_all_kind_prompts_share_output_contract():
    for kind in ReportKind:
        prompt = _build_system_prompt(kind)
        assert '"findings"' in prompt
        assert "valid JSON object" in prompt


@pytest.mark.asyncio
async def test_explanation_request_uses_explanation_prompt(monkeypatch, tmp_repo):
    captured = {"system": ""}

    async def capture(role, request):
        captured["system"] = request.messages[0].content
        return LLMResponse(text=VALID_ANALYZER_JSON, provider="fake", model="m")

    _patch_dependencies(monkeypatch, tmp_repo)
    monkeypatch.setattr(report_analyzer, "complete_for_role", capture)

    result = await run_report_analysis(
        str(uuid.uuid4()), "proj-x", "explain this project"
    )

    assert result.report_result.report_kind == "project_explanation"
    system_prompt = captured["system"].lower()
    assert "architecture" in system_prompt
    assert "do not turn this into a code audit" in system_prompt


@pytest.mark.asyncio
async def test_issue_request_uses_issue_review_prompt(monkeypatch, tmp_repo):
    captured = {"system": ""}

    async def capture(role, request):
        captured["system"] = request.messages[0].content
        return LLMResponse(text=VALID_ANALYZER_JSON, provider="fake", model="m")

    _patch_dependencies(monkeypatch, tmp_repo)
    monkeypatch.setattr(report_analyzer, "complete_for_role", capture)

    result = await run_report_analysis(
        str(uuid.uuid4()), "proj-x", "is there any issue in the code"
    )

    assert result.report_result.report_kind == "issue_review"
    assert "bug" in captured["system"].lower()


@pytest.mark.asyncio
async def test_feature_request_uses_feature_discovery_prompt(monkeypatch, tmp_repo):
    captured = {"system": ""}

    async def capture(role, request):
        captured["system"] = request.messages[0].content
        return LLMResponse(text=VALID_ANALYZER_JSON, provider="fake", model="m")

    _patch_dependencies(monkeypatch, tmp_repo)
    monkeypatch.setattr(report_analyzer, "complete_for_role", capture)

    result = await run_report_analysis(
        str(uuid.uuid4()), "proj-x", "what features can we add to this project"
    )

    assert result.report_result.report_kind == "feature_discovery"
    assert "feature" in captured["system"].lower()


def test_build_limited_report_is_read_only_and_sanitized():
    report = build_limited_report(
        "review repo",
        "boom with key AIzaSyA123456789012345678901234567890",
    )
    assert READ_ONLY_NOTE in report
    assert "AIzaSyA123456789012345678901234567890" not in report
    assert "[REDACTED]" in report


def test_report_analyzer_does_not_import_mutation_modules():
    source = Path(report_analyzer.__file__).read_text(encoding="utf-8")
    forbidden = [
        "import backend.pipeline.coder",
        "from backend.pipeline.coder",
        "patch_applier",
        "from backend.pipeline.tester",
        "import backend.pipeline.tester",
        "local_git",
        "pr_orchestrator",
        "github_client",
        "chunked_orchestrator",
        "approve_chunk_and_commit",
        "push_and_create_pr",
        "execute_approved_chunks",
    ]
    for needle in forbidden:
        assert needle not in source, f"report_analyzer must not reference {needle}"
