"""
Tests for the deterministic high-risk gate (risk_scanner.py).

Pure unit tests. No DB, no FastAPI, no LLM.
"""

import pytest

from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import risk_scanner
from backend.pipeline.risk_scanner import (
    chunk_is_high_risk,
    scan_triage_result,
)

pytestmark = pytest.mark.unit


def _chunk(
    *,
    chunk_number: int = 1,
    title: str = "Add docs",
    description: str = "Update README.",
    files_expected: list[str] | None = None,
    risk_level: str = "low",
    requires_human_review: bool = False,
    depends_on: list[int] | None = None,
) -> ChunkDefinition:
    if files_expected is None:
        files_expected = ["README.md"]
    return ChunkDefinition(
        chunk_number=chunk_number,
        title=title,
        description=description,
        files_expected=files_expected,
        depends_on=depends_on or [],
        risk_level=risk_level,
        token_estimate=100,
        requires_human_review=requires_human_review,
        rationale="test",
    )


def _triage(chunks: list[ChunkDefinition]) -> TriageResult:
    return TriageResult(
        run_id="run-1",
        project_id="proj-1",
        feature_description="test feature",
        complexity="easy" if len(chunks) == 1 else "medium",
        total_chunks=len(chunks),
        chunks=chunks,
        reasoning="test plan",
    )


def test_alembic_migration_chunk_becomes_high_risk():
    chunk = _chunk(
        title="Alembic migration",
        description="Run alembic migration to alter table users.",
        files_expected=["backend/db/migrations/0042_users.py"],
    )

    result = scan_triage_result(_triage([chunk]))

    upgraded = result.chunks[0]
    assert upgraded.risk_level == "high"
    assert upgraded.requires_human_review is True


def test_auth_keyword_in_description_becomes_high_risk():
    chunk = _chunk(
        title="Refresh sessions",
        description="Rotate jwt and refresh user permissions.",
        files_expected=["backend/services/users.py"],
    )

    upgraded = scan_triage_result(_triage([chunk])).chunks[0]

    assert upgraded.risk_level == "high"
    assert upgraded.requires_human_review is True


def test_secret_or_env_path_becomes_high_risk():
    chunk = _chunk(
        title="Update sample env",
        description="Add new variable.",
        files_expected=["config/.env.example"],
    )

    upgraded = scan_triage_result(_triage([chunk])).chunks[0]

    assert upgraded.risk_level == "high"


def test_checkpoint_or_local_git_path_becomes_high_risk():
    chunk_git = _chunk(
        title="Tweak helper",
        description="Adjust helper logic.",
        files_expected=["backend/git/local_git.py"],
    )
    chunk_checkpoint = _chunk(
        title="Tweak helper",
        description="Adjust resume rollback flow.",
        files_expected=["backend/checkpoint/checkpoint_store.py"],
    )

    assert chunk_is_high_risk(chunk_git) is True
    assert chunk_is_high_risk(chunk_checkpoint) is True


def test_workflow_or_lockfile_path_becomes_high_risk():
    chunk_workflow = _chunk(
        title="Update CI",
        description="Pin runner.",
        files_expected=[".github/workflows/ci.yml"],
    )
    chunk_lock = _chunk(
        title="Bump deps",
        description="Update lockfile.",
        files_expected=["package-lock.json"],
    )

    assert chunk_is_high_risk(chunk_workflow) is True
    assert chunk_is_high_risk(chunk_lock) is True


def test_backend_routes_path_becomes_high_risk():
    chunk = _chunk(
        title="Tweak handler",
        description="Adjust handler logic.",
        files_expected=["backend/routes/foo.py"],
    )

    upgraded = scan_triage_result(_triage([chunk])).chunks[0]

    assert upgraded.risk_level == "high"
    assert upgraded.requires_human_review is True


def test_already_high_chunk_is_preserved_unchanged():
    chunk = _chunk(
        title="Docs only",
        description="Update README.",
        files_expected=["README.md"],
        risk_level="high",
        requires_human_review=True,
    )

    result = scan_triage_result(_triage([chunk])).chunks[0]

    assert result.risk_level == "high"
    assert result.requires_human_review is True
    assert result is chunk or result.model_dump() == chunk.model_dump()


def test_clean_docs_chunk_remains_unchanged():
    chunk = _chunk(
        title="Tweak prose",
        description="Improve wording in onboarding doc.",
        files_expected=["docs/onboarding.md"],
    )

    result = scan_triage_result(_triage([chunk])).chunks[0]

    assert result.risk_level == "low"
    assert result.requires_human_review is False


def test_scanner_never_downgrades_high_risk_with_clean_text():
    chunk = _chunk(
        title="Docs only",
        description="No keywords here.",
        files_expected=["README.md"],
        risk_level="high",
        requires_human_review=True,
    )

    result = scan_triage_result(_triage([chunk])).chunks[0]

    assert result.risk_level == "high"
    assert result.requires_human_review is True


def test_empty_files_expected_is_treated_as_high_risk():
    chunk = _chunk(
        title="Loose chunk",
        description="No scope defined.",
        files_expected=[],
    )

    result = scan_triage_result(_triage([chunk])).chunks[0]

    assert result.risk_level == "high"
    assert result.requires_human_review is True


def test_scanner_failure_fails_safe_to_high_risk(monkeypatch):
    chunk = _chunk(
        title="Docs only",
        description="Update README.",
        files_expected=["README.md"],
    )

    def boom(_chunk):
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(risk_scanner, "chunk_is_high_risk", boom)

    result = scan_triage_result(_triage([chunk])).chunks[0]

    assert result.risk_level == "high"
    assert result.requires_human_review is True


def test_mixed_plan_only_upgrades_matching_chunks():
    safe_chunk = _chunk(
        chunk_number=1,
        title="Docs",
        description="Update onboarding.",
        files_expected=["docs/intro.md"],
    )
    risky_chunk = _chunk(
        chunk_number=2,
        title="Tweak login",
        description="Adjust password reset flow.",
        files_expected=["backend/services/users.py"],
        depends_on=[1],
    )

    result = scan_triage_result(_triage([safe_chunk, risky_chunk]))

    assert result.chunks[0].risk_level == "low"
    assert result.chunks[0].requires_human_review is False
    assert result.chunks[1].risk_level == "high"
    assert result.chunks[1].requires_human_review is True
