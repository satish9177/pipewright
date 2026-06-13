"""
test_handoff_models.py
Unit tests for pipeline handoff model contracts.
"""

import pytest
from pydantic import ValidationError

from backend.models.handoff import (
    CoderHandoff,
    FileChange,
    PlannerHandoff,
    SuggestedMemoryEntry,
)

pytestmark = pytest.mark.unit


def _file_change() -> FileChange:
    return FileChange(
        path="backend/example.py",
        action="create",
        content="print('ok')\n",
        reason="test change",
    )


def _coder_handoff(entries) -> CoderHandoff:
    return CoderHandoff(
        run_id="run-structured-memory",
        feature_description="Add structured memory suggestions",
        files_changed=[_file_change()],
        summary="Implemented the change.",
        suggested_memory_entries=entries,
    )


def test_coder_handoff_accepts_structured_memory_entries():
    handoff = _coder_handoff([
        {
            "content": " Scope guard remains authoritative for approved files. ",
            "category": "security",
            "scope": "backend",
            "rationale": "Durable safety convention.",
        }
    ])

    entry = handoff.suggested_memory_entries[0]
    assert isinstance(entry, SuggestedMemoryEntry)
    assert entry.content == "Scope guard remains authoritative for approved files."
    assert entry.category == "security"
    assert entry.scope == "backend"
    assert entry.rationale == "Durable safety convention."


def test_coder_handoff_accepts_legacy_string_memory_entries():
    handoff = _coder_handoff(["Run tests with pytest -q."])

    entry = handoff.suggested_memory_entries[0]
    assert isinstance(entry, SuggestedMemoryEntry)
    assert entry.content == "Run tests with pytest -q."
    assert entry.category == "other"
    assert entry.scope == "global"
    assert entry.rationale is None


def test_structured_and_legacy_memory_entries_round_trip_through_model_dump():
    handoff = _coder_handoff([
        {
            "content": "Never commit secrets to .env.",
            "category": "security",
            "scope": "backend",
            "rationale": "Protects future runs.",
        },
        "Run tests with pytest -q.",
    ])

    dumped = handoff.model_dump()
    restored = CoderHandoff.model_validate(dumped)

    assert restored.model_dump() == dumped
    assert [entry.content for entry in restored.suggested_memory_entries] == [
        "Never commit secrets to .env.",
        "Run tests with pytest -q.",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"content": ""},
        {"content": "   "},
        {"content": 123},
        123,
    ],
)
def test_coder_handoff_rejects_missing_blank_or_non_string_memory_content(entry):
    with pytest.raises(ValidationError):
        _coder_handoff([entry])


def test_coder_handoff_coerces_invalid_category_and_scope():
    handoff = _coder_handoff([
        {
            "content": "Project prefers concise review notes.",
            "category": "nonsense",
            "scope": "desktop",
        }
    ])

    entry = handoff.suggested_memory_entries[0]
    assert entry.category == "other"
    assert entry.scope == "global"


def test_planner_handoff_suggested_memory_entries_remain_strings_only():
    planner = PlannerHandoff(
        run_id="planner-run",
        feature_description="Plan work",
        goal="Plan the change",
        steps=["Read code"],
        suggested_memory_entries=["planner memory stays legacy"],
    )
    assert planner.suggested_memory_entries == ["planner memory stays legacy"]

    with pytest.raises(ValidationError):
        PlannerHandoff(
            run_id="planner-run",
            feature_description="Plan work",
            goal="Plan the change",
            steps=["Read code"],
            suggested_memory_entries=[{"content": "do not change planner"}],
        )
