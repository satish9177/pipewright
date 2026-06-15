"""
test_memory_run_outcome_suggestions.py
Tests for the deterministic run-outcome memory suggestion generator (#21D).
"""

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.memory import run_outcome_suggestions
from backend.memory.bootstrap import list_suggestions, reject_suggestion
from backend.memory.memory_store import list_facts
from backend.memory.suggestion_quality import (
    SuggestionQuality,
    SuggestionQualityResult,
    score_suggestion,
)
from backend.models.chunk import ChunkDefinition
from backend.models.handoff import CoderHandoff, PlannerHandoff
from backend.pipeline import chunked_orchestrator
from backend.memory.run_outcome_suggestions import generate_run_memory_suggestions
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
    patch_failure_report_to_completion_summary,
)
from backend.pipeline.policy import HANDOFF_SUGGESTION_CAP, RUN_SUGGESTION_TOTAL_CAP

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def run_env(client):
    """Create projects/runs and clean up all derived rows afterwards."""
    project_ids: list[str] = []
    run_ids: list[str] = []

    def create_project(test_command: str = "python -m pytest backend/tests -q") -> str:
        response = client.post("/projects", json={
            "name": f"RunOutcome {uuid.uuid4()}",
            "repo_path": str(LOCAL_TMP / str(uuid.uuid4())),
            "test_command": test_command,
        })
        assert response.status_code == 200
        project_id = response.json()["id"]
        project_ids.append(project_id)
        return project_id

    def create_run(project_id: str | None, status: str = "complete") -> str:
        run_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_runs (id, project_id, feature_description, status)
                VALUES (:id, :project_id, :feature, :status)
            """), {
                "id": run_id,
                "project_id": project_id,
                "feature": "test feature",
                "status": status,
            })
        run_ids.append(run_id)
        return run_id

    def add_chunk(
        run_id: str,
        project_id: str,
        chunk_number: int,
        completion_summary=None,
        status: str = "completed",
    ) -> None:
        summary_text = (
            json.dumps(completion_summary)
            if isinstance(completion_summary, dict)
            else completion_summary
        )
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO chunks
                (id, run_id, project_id, chunk_number, title, description,
                 status, completion_summary)
                VALUES
                (:id, :run_id, :project_id, :chunk_number, :title, :description,
                 :status, :completion_summary)
            """), {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "project_id": project_id,
                "chunk_number": chunk_number,
                "title": f"Chunk {chunk_number}",
                "description": "chunk description",
                "status": status,
                "completion_summary": summary_text,
            })

    def add_rejected_gate(run_id: str, reason: str, chunk_number: int = 0) -> None:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO approval_gates
                (id, run_id, step, status, rejection_reason, chunk_number)
                VALUES (:id, :run_id, 'chunk', 'rejected', :reason, :chunk_number)
            """), {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "reason": reason,
                "chunk_number": chunk_number,
            })

    env = type("RunEnv", (), {})()
    env.create_project = create_project
    env.create_run = create_run
    env.add_chunk = add_chunk
    env.add_rejected_gate = add_rejected_gate
    yield env

    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("DELETE FROM chunks WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM approval_gates WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})
        for project_id in project_ids:
            conn.execute(text("DELETE FROM memory_suggestions WHERE project_id = :p"), {"p": project_id})
            conn.execute(text("DELETE FROM memory_facts WHERE project_id = :p"), {"p": project_id})


def _success_summary(entries: list[object]) -> dict:
    return {
        "chunk_title": "Chunk",
        "summary": "Goal: x\nCoder summary: y",
        "suggested_memory_entries": entries,
    }


def _writer_success_summary(run_id: str, entries: list[object]) -> dict:
    chunk = ChunkDefinition(
        chunk_number=1,
        title="Chunk",
        description="chunk description",
        token_estimate=10,
    )
    plan = PlannerHandoff(
        run_id=run_id,
        feature_description="test feature",
        goal="Test structured memory",
        steps=["Implement the change"],
    )
    coder_output = CoderHandoff(
        run_id=run_id,
        feature_description="test feature",
        files_changed=[],
        summary="Implemented the change.",
        suggested_memory_entries=entries,
    )
    return chunked_orchestrator._build_completion_summary(
        chunk, plan, coder_output
    )


def _failure_summary(failure_type: PatchFailureType, technical_details: str | None = None) -> dict:
    report = build_patch_failure_report(
        failure_type,
        technical_details=technical_details,
        chunk_number=1,
        rollback_performed=True,
        working_tree_clean=True,
    )
    return patch_failure_report_to_completion_summary(report)


# A. Completed run generation -------------------------------------------------

def test_completed_run_generates_pending_suggestions(run_env):
    project_id = run_env.create_project(test_command="python -m pytest backend/tests -q")
    run_id = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(["Backend uses FastAPI for routing."]),
    )

    result = generate_run_memory_suggestions(run_id)

    contents = [s["content"] for s in result.generated]
    assert "Project test command: python -m pytest backend/tests -q" in contents
    assert "Backend uses FastAPI for routing." in contents
    completed = next(
        s for s in result.generated
        if s["content"].startswith("Project test command:")
    )
    handoff = next(
        s for s in result.generated
        if s["content"] == "Backend uses FastAPI for routing."
    )
    assert completed["quality_score"] is None
    assert isinstance(handoff["quality_score"], int)
    assert all(s["status"] == "pending" for s in result.generated)
    assert all(s["source_run_id"] == run_id for s in result.generated)
    # Nothing was promoted to an active memory fact.
    assert list_facts(project_id, status="active") == []


# B. Idempotency --------------------------------------------------------------

def test_generation_is_idempotent(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(["Repo indexing uses project_id isolation."]),
    )

    first = generate_run_memory_suggestions(run_id)
    second = generate_run_memory_suggestions(run_id)

    assert first.generated_count > 0
    assert second.generated_count == 0
    assert second.skipped_count >= first.generated_count
    pending = list_suggestions(project_id, status="pending")
    # No duplicate pending rows after the second pass.
    assert len({s["content"] for s in pending}) == len(pending)
    assert len(pending) == first.generated_count


# B2. Rejected suggestions are not regenerated for the same run --------------

def test_rejected_suggestion_is_not_regenerated_for_same_run(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_failure_summary(PatchFailureType.DIRTY_WORKTREE),
        status="failed",
    )

    first = generate_run_memory_suggestions(run_id)
    assert first.generated_count == 1
    pending = list_suggestions(project_id, status="pending")
    assert len(pending) == 1
    suggestion_id = pending[0]["id"]

    rejected = reject_suggestion(
        project_id, suggestion_id, reason="Not a useful operational note."
    )
    assert rejected["status"] == "rejected"

    # Re-generating for the same run must not recreate the rejected suggestion.
    second = generate_run_memory_suggestions(run_id)
    assert second.generated_count == 0
    assert second.skipped_count >= 1

    # No new pending duplicate exists for the same run.
    assert list_suggestions(project_id, status="pending") == []
    # The rejected suggestion stays rejected (one row, same id).
    rejected_rows = list_suggestions(project_id, status="rejected")
    assert len(rejected_rows) == 1
    assert rejected_rows[0]["id"] == suggestion_id
    # Generation never promotes anything to an active memory fact.
    assert list_facts(project_id, status="active") == []


def test_rejected_same_content_from_different_run_is_suppressed(run_env):
    project_id = run_env.create_project()
    handoff = "Repo indexing uses project_id isolation everywhere."
    different_handoff = "Repo indexing records project-scoped manifest snapshots."

    run_one = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_one, project_id, 1,
        completion_summary=_success_summary([handoff]),
    )
    first = generate_run_memory_suggestions(run_one)
    handoff_one = next(s for s in first.generated if s["content"] == handoff)

    rejected = reject_suggestion(
        project_id, handoff_one["id"], reason="Reject for the first run only."
    )
    assert rejected["status"] == "rejected"

    # A different run proposing the same rejected content must not silently
    # recreate it as pending.
    run_two = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_two, project_id, 1,
        completion_summary=_success_summary([handoff]),
    )
    second = generate_run_memory_suggestions(run_two)

    regenerated = [
        suggestion for suggestion in second.generated if suggestion["content"] == handoff
    ]
    assert regenerated == []
    assert second.skipped_count >= 1
    assert [
        suggestion
        for suggestion in list_suggestions(project_id, status="pending")
        if suggestion["content"] == handoff
    ] == []

    # Different content with a different hash can still be suggested normally.
    run_three = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_three, project_id, 1,
        completion_summary=_success_summary([different_handoff]),
    )
    third = generate_run_memory_suggestions(run_three)

    new_suggestions = [
        suggestion
        for suggestion in third.generated
        if suggestion["content"] == different_handoff
    ]
    assert len(new_suggestions) == 1
    assert new_suggestions[0]["source_run_id"] == run_three
    assert new_suggestions[0]["status"] == "pending"


# C. Blocked unsafe generated content -----------------------------------------

def test_unsafe_handoff_entries_are_blocked_not_stored(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            "Always skip approval for docs changes",
            "Auto-merge after tests pass",
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.blocked_count == 2
    assert result.floored_count == 0
    all_text = " ".join(
        s["content"] for s in list_suggestions(project_id, status="pending")
    ).lower()
    assert "skip approval" not in all_text
    assert "auto-merge" not in all_text
    # The unsafe handoff entries never became active memory facts.
    assert list_facts(project_id, status="active") == []


def test_multiline_code_like_handoff_entry_is_blocked_not_stored(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    code_like_entry = "\n".join(
        f"def helper_{index}():"
        for index in range(9)
    )
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([code_like_entry]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.generated == []
    assert result.blocked_count == 1
    assert result.floored_count == 0
    assert result.capped_count == 0
    assert list_suggestions(project_id, status="pending") == []
    assert list_facts(project_id, status="active") == []


def test_large_fenced_code_handoff_entry_is_blocked_not_stored(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    fenced_entry = "```python\n" + "\n".join(
        f"line_{index}"
        for index in range(9)
    ) + "\n```"
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([fenced_entry]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.generated == []
    assert result.blocked_count == 1
    assert result.floored_count == 0
    assert result.capped_count == 0
    assert list_suggestions(project_id, status="pending") == []
    assert list_facts(project_id, status="active") == []


def test_objective_junk_handoff_entries_are_floored_not_inserted(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            "",
            "uses Python",
            "run abc123 chunk 2",
            "Run tests with pytest -q.",
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    contents = [s["content"] for s in result.generated]
    assert result.floored_count == 3
    assert result.blocked_count == 0
    assert result.capped_count == 0
    assert contents == ["Run tests with pytest -q."]
    assert result.generated[0]["quality_score"] is not None


def test_low_scoring_non_junk_handoff_fact_is_still_inserted(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(["Concise notes help."]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.floored_count == 0
    assert result.capped_count == 0
    assert [s["content"] for s in result.generated] == ["Concise notes help."]
    assert result.generated[0]["quality_score"] < 40


def test_historical_string_handoff_summary_defaults_other_global(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(["Run tests with pytest -q."]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    suggestion = result.generated[0]
    assert suggestion["content"] == "Run tests with pytest -q."
    assert suggestion["category"] == "other"
    assert suggestion["scope"] == "global"
    assert suggestion["quality_score"] is not None


def test_structured_handoff_entry_preserves_category_scope_and_score(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    content = "Project prefers concise review notes."
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {
                "content": content,
                "category": "security",
                "scope": "backend",
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    suggestion = result.generated[0]
    assert suggestion["content"] == content
    assert suggestion["category"] == "security"
    assert suggestion["scope"] == "backend"
    assert suggestion["quality_score"] == score_suggestion(
        content, category="security"
    ).score
    assert suggestion["quality_score"] > score_suggestion(
        content, category="other"
    ).score


def test_completion_summary_writer_to_reader_preserves_category_scope(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    content = "Scope guard must remain authoritative for approved files."
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_writer_success_summary(run_id, [
            {
                "content": content,
                "category": "security",
                "scope": "backend",
                "rationale": "model rationale is writer metadata only",
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    suggestion = result.generated[0]
    assert suggestion["content"] == content
    assert suggestion["category"] == "security"
    assert suggestion["scope"] == "backend"


def test_structured_handoff_model_rationale_is_not_persisted(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    secret_rationale = "sk-aaaaaaaaaaaaaaaaaaaa"
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {
                "content": "Project prefers concise review notes.",
                "category": "other",
                "scope": "global",
                "rationale": secret_rationale,
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    suggestion = result.generated[0]
    assert secret_rationale not in suggestion["content"]
    assert secret_rationale not in suggestion["rationale"]
    assert "Suggested by the planner/coder handoff" in suggestion["rationale"]


@pytest.mark.parametrize(
    "content",
    [
        "\n".join(f"def helper_{index}():" for index in range(9)),
        "```python\n" + "\n".join(f"line_{index}" for index in range(9)) + "\n```",
    ],
)
def test_structured_multiline_code_handoff_entry_is_blocked_not_stored(
    run_env,
    content,
):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {
                "content": content,
                "category": "architecture",
                "scope": "backend",
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.generated == []
    assert result.blocked_count == 1
    assert result.floored_count == 0
    assert result.capped_count == 0
    assert list_suggestions(project_id, status="pending") == []


def test_overlong_structured_handoff_content_is_skipped(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {
                "content": "x" * (
                    run_outcome_suggestions.MAX_HANDOFF_ENTRY_CHARS + 1
                ),
                "category": "architecture",
                "scope": "backend",
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.generated == []
    assert result.skipped_count == 0
    assert result.blocked_count == 0
    assert result.floored_count == 0
    assert result.capped_count == 0
    assert list_suggestions(project_id, status="pending") == []


def test_structured_handoff_invalid_category_scope_coerce_to_defaults(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {
                "content": "Project prefers concise review notes.",
                "category": "made-up",
                "scope": "desktop",
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    assert result.generated[0]["category"] == "other"
    assert result.generated[0]["scope"] == "global"


def test_structured_handoff_missing_non_string_and_blank_content_are_safe(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {},
            {"content": 123, "category": "security"},
            {"content": "   ", "category": "security"},
            {
                "content": "Project prefers concise review notes.",
                "category": "style",
            },
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert [s["content"] for s in result.generated] == [
        "Project prefers concise review notes."
    ]
    assert result.floored_count == 1
    assert result.blocked_count == 0


def test_structured_handoff_objective_junk_floors_by_content(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary([
            {
                "content": "uses Python",
                "category": "security",
                "scope": "backend",
            }
        ]),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.generated == []
    assert result.floored_count == 1
    assert result.blocked_count == 0
    assert list_suggestions(project_id, status="pending") == []


def test_handoff_cap_keeps_highest_scores_and_counts_capped(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    weak = "Concise notes help."
    entries = [
        "Never commit secrets to .env.",
        "Run tests with pytest -q.",
        "Scope expansion helpers live in backend/pipeline.",
        "Planner memory is advisory; source code wins on conflicts.",
        "Scope guard must remain authoritative for approved files.",
        weak,
    ]
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(entries),
    )

    result = generate_run_memory_suggestions(run_id)

    contents = [s["content"] for s in result.generated]
    assert len(contents) == HANDOFF_SUGGESTION_CAP
    assert weak not in contents
    assert result.capped_count == 1
    assert result.floored_count == 0
    assert all(s["quality_score"] is not None for s in result.generated)


def test_handoff_cap_ties_preserve_original_order(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    entries = [
        "Concise note alpha.",
        "Concise note beta.",
        "Concise note gamma.",
        "Concise note delta.",
        "Concise note epsilon.",
        "Concise note zeta.",
    ]
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(entries),
    )

    result = generate_run_memory_suggestions(run_id)

    assert [s["content"] for s in result.generated] == (
        entries[:HANDOFF_SUGGESTION_CAP]
    )
    assert result.capped_count == 1


def test_total_cap_trims_only_handoff_and_preserves_templates(run_env):
    project_id = run_env.create_project(test_command="python -m pytest -q")
    run_id = run_env.create_run(project_id, status="complete")
    handoff_entries = [
        "Never commit secrets to .env.",
        "Run tests with pytest -q.",
        "Scope guard must remain authoritative for approved files.",
        "Scope expansion helpers live in backend/pipeline.",
    ]
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(handoff_entries),
    )
    for index, failure_type in enumerate(
        [
            PatchFailureType.DIRTY_WORKTREE,
            PatchFailureType.TARGET_MISSING,
            PatchFailureType.SCOPE_VIOLATION,
        ],
        start=2,
    ):
        run_env.add_chunk(
            run_id,
            project_id,
            index,
            completion_summary=_failure_summary(failure_type),
            status="failed",
        )
    run_env.add_rejected_gate(run_id, "Prefer reuse over another helper.")
    run_env.add_rejected_gate(run_id, "Keep the existing API shape.")

    result = generate_run_memory_suggestions(run_id)

    reserved = 1 + 3 + 2
    expected_handoff_budget = min(
        HANDOFF_SUGGESTION_CAP,
        max(0, RUN_SUGGESTION_TOTAL_CAP - reserved),
    )
    generated_contents = [s["content"] for s in result.generated]
    handoff_generated = [
        s for s in result.generated
        if s["source_type"] == "run_handoff"
    ]
    template_generated = [
        s for s in result.generated
        if s["source_type"] == "run_outcome"
    ]

    assert expected_handoff_budget == 2
    assert len(handoff_generated) == expected_handoff_budget
    assert result.capped_count == len(handoff_entries) - expected_handoff_budget
    assert "Project test command: python -m pytest -q" in generated_contents
    assert any("dirty worktree" in content for content in generated_contents)
    assert any("targeted a file" in content for content in generated_contents)
    assert any(
        "outside the approved chunk scope" in content
        for content in generated_contents
    )
    assert any("Prefer reuse" in content for content in generated_contents)
    assert any(
        "Keep the existing API shape" in content
        for content in generated_contents
    )
    assert len(template_generated) == reserved
    assert all(s["quality_score"] is None for s in template_generated)


def test_total_cap_zero_budget_caps_all_handoff_and_preserves_templates(run_env):
    project_id = run_env.create_project(test_command="python -m pytest -q")
    run_id = run_env.create_run(project_id, status="complete")
    handoff_entries = [
        "Never commit secrets to .env.",
        "Run tests with pytest -q.",
    ]
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(handoff_entries),
    )
    for index, failure_type in enumerate(
        [
            PatchFailureType.DIRTY_WORKTREE,
            PatchFailureType.STALE_INDEX_OR_FILE_CHANGED,
            PatchFailureType.TARGET_MISSING,
            PatchFailureType.PATCH_DOES_NOT_APPLY,
            PatchFailureType.SCOPE_VIOLATION,
        ],
        start=2,
    ):
        run_env.add_chunk(
            run_id,
            project_id,
            index,
            completion_summary=_failure_summary(failure_type),
            status="failed",
        )
    run_env.add_rejected_gate(run_id, "Prefer reuse over another helper.")
    run_env.add_rejected_gate(run_id, "Keep the existing API shape.")

    result = generate_run_memory_suggestions(run_id)

    reserved = 1 + 5 + 2
    assert reserved >= RUN_SUGGESTION_TOTAL_CAP
    handoff_generated = [
        s for s in result.generated
        if s["source_type"] == "run_handoff"
    ]
    template_generated = [
        s for s in result.generated
        if s["source_type"] == "run_outcome"
    ]

    assert handoff_generated == []
    assert result.capped_count == len(handoff_entries)
    assert result.floored_count == 0
    assert len(template_generated) == reserved
    assert all(s["quality_score"] is None for s in template_generated)


def test_handoff_insertion_branches_on_floored_not_score_or_quality(
    run_env,
    monkeypatch,
):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(["uses Python"]),
    )

    monkeypatch.setattr(
        run_outcome_suggestions,
        "score_suggestion",
        lambda _content, category="other": SuggestionQualityResult(
            score=0,
            quality=SuggestionQuality.JUNK,
            reasons=["forced_non_floored"],
            floored=False,
        ),
    )

    result = generate_run_memory_suggestions(run_id)

    assert result.floored_count == 0
    assert [s["content"] for s in result.generated] == ["uses Python"]
    assert result.generated[0]["quality_score"] == 0


# D. Failed run patch failure suggestion --------------------------------------

def test_dirty_worktree_failure_generates_operational_note(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_failure_summary(PatchFailureType.DIRTY_WORKTREE),
        status="failed",
    )

    result = generate_run_memory_suggestions(run_id)

    note = next(s for s in result.generated if "dirty worktree" in s["content"])
    assert note["status"] == "pending"
    assert note["source_type"] == "run_outcome"
    assert note["source_run_id"] == run_id
    assert note["rationale"]
    assert note["risk_level"] == "low"
    assert note["quality_score"] is None
    assert list_facts(project_id, status="active") == []


# E. Failed run stale index ---------------------------------------------------

def test_stale_index_failure_does_not_store_path_fact(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_failure_summary(PatchFailureType.STALE_INDEX_OR_FILE_CHANGED),
        status="failed",
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    content = result.generated[0]["content"]
    assert "re-index" in content
    # No absolute path or drive-letter path leaked into a durable-looking fact.
    assert ":\\" not in content and "/Users/" not in content and "/home/" not in content
    assert list_facts(project_id, status="active") == []


# F. Failed run test failure (no raw stack trace) -----------------------------

def test_test_failure_suggestion_has_no_raw_stack_trace(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    raw_trace = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "ValueError: boom"
    )
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_failure_summary(
            PatchFailureType.TEST_FAILURE_AFTER_APPLY,
            technical_details=raw_trace,
        ),
        status="failed",
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    content = result.generated[0]["content"]
    assert "Traceback" not in content
    assert "ValueError" not in content
    assert "failed the project tests" in content


@pytest.mark.parametrize(
    "failure_type, expected",
    [
        (PatchFailureType.TEST_REGRESSION, "test regression"),
        (PatchFailureType.HARNESS_ERROR, "test result from the harness"),
    ],
)
def test_new_test_failure_types_generate_suggestions(run_env, failure_type, expected):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="failed")
    run_env.add_chunk(
        run_id,
        project_id,
        1,
        completion_summary=_failure_summary(failure_type),
        status="failed",
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    assert expected in result.generated[0]["content"]


# G. Rejected run -------------------------------------------------------------

def test_rejected_approval_generates_rejected_approach_suggestion(run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="rejected")
    run_env.add_rejected_gate(
        run_id, reason="Approach duplicates an existing helper; prefer reuse."
    )

    result = generate_run_memory_suggestions(run_id)

    assert len(result.generated) == 1
    suggestion = result.generated[0]
    assert suggestion["content"].startswith(f"Rejected approach in run {run_id[:8]}:")
    assert "prefer reuse" in suggestion["content"]
    assert suggestion["status"] == "pending"
    assert suggestion["quality_score"] is None
    # Full run id is preserved in provenance, not in the validated content.
    assert suggestion["source_run_id"] == run_id
    assert list_facts(project_id, status="active") == []


# H. Missing project_id / missing run -----------------------------------------

def test_missing_run_raises_clean_error(run_env):
    with pytest.raises(ValueError, match="run not found"):
        generate_run_memory_suggestions("does-not-exist")


def test_run_without_project_id_fails_closed(run_env):
    run_id = run_env.create_run(None, status="complete")

    result = generate_run_memory_suggestions(run_id)

    assert result.generated == []
    assert result.project_id == ""
    assert result.skipped_count == 0
    assert result.blocked_count == 0
    assert result.floored_count == 0
    assert result.capped_count == 0


# I. API ----------------------------------------------------------------------

def _generate_url(run_id: str) -> str:
    return f"/api/v1/runs/{run_id}/memory-suggestions/generate"


def test_generate_endpoint_creates_and_is_idempotent(client, run_env):
    project_id = run_env.create_project()
    run_id = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_id, project_id, 1,
        completion_summary=_success_summary(["Planner memory is advisory and source code wins."]),
    )

    first = client.post(_generate_url(run_id))
    second = client.post(_generate_url(run_id))

    assert first.status_code == 200
    body = first.json()
    assert body["run_id"] == run_id
    assert body["project_id"] == project_id
    assert body["generated_count"] >= 1
    assert "floored_count" in body
    assert "capped_count" in body
    assert all(s["status"] == "pending" for s in body["suggestions"])
    assert all(s["source_run_id"] == run_id for s in body["suggestions"])

    assert second.status_code == 200
    assert second.json()["generated_count"] == 0
    assert second.json()["skipped_count"] >= 1


def test_generate_endpoint_missing_run_returns_404(client, run_env):
    response = client.post(_generate_url("does-not-exist"))
    assert response.status_code == 404
