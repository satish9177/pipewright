"""
test_suggestion_quality.py
Unit tests for the deterministic handoff suggestion quality scorer (M5 7-alpha).

Pure string scoring: no execution, no filesystem, no DB.
"""

import pytest

from backend.memory.suggestion_quality import (
    SuggestionQuality,
    is_objective_junk,
    score_suggestion,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("content", ["", "   ", "\t\n"])
def test_empty_or_blank_is_floored_junk(content):
    result = score_suggestion(content)

    assert is_objective_junk(content) is True
    assert result.floored is True
    assert result.quality is SuggestionQuality.JUNK


@pytest.mark.parametrize(
    "content",
    [
        "run abc123",
        "chunk 2",
        "attempt 1",
        "run abc123 chunk 2",
        "run abc123 failed",
    ],
)
def test_pure_run_chunk_attempt_references_are_floored_junk(content):
    result = score_suggestion(content)

    assert is_objective_junk(content) is True
    assert result.floored is True
    assert result.quality is SuggestionQuality.JUNK


@pytest.mark.parametrize(
    "content",
    [
        "uses Python",
        "Backend uses FastAPI.",
        "has tests",
    ],
)
def test_strict_low_information_denylist_is_floored_junk(content):
    result = score_suggestion(content)

    assert is_objective_junk(content) is True
    assert result.floored is True
    assert result.quality is SuggestionQuality.JUNK


@pytest.mark.parametrize(
    "content",
    [
        "Never commit secrets to .env.",
        "Run tests with pytest -q.",
    ],
)
def test_durable_safety_and_command_facts_are_strong(content):
    result = score_suggestion(content)

    assert result.floored is False
    assert result.quality is SuggestionQuality.STRONG
    assert result.score >= 70


def test_durable_location_fact_is_ok_or_strong():
    result = score_suggestion("Scope expansion helpers live in backend/pipeline.")

    assert result.floored is False
    assert result.quality in {SuggestionQuality.OK, SuggestionQuality.STRONG}


def test_borderline_project_specific_fact_is_not_floored():
    result = score_suggestion("Project prefers concise review notes.")

    assert is_objective_junk("Project prefers concise review notes.") is False
    assert result.floored is False
    assert result.quality is not SuggestionQuality.JUNK


def test_low_scoring_non_junk_remains_weak_not_floored():
    result = score_suggestion("Concise notes help.")

    assert result.score < 40
    assert result.floored is False
    assert result.quality is SuggestionQuality.WEAK


def test_scoring_is_deterministic():
    first = score_suggestion("Run tests with pytest -q.")
    second = score_suggestion("Run tests with pytest -q.")

    assert first == second
