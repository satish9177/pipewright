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
    build_limited_report,
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
