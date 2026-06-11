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
from backend.memory.bootstrap import list_suggestions, reject_suggestion
from backend.memory.memory_store import list_facts
from backend.memory.run_outcome_suggestions import generate_run_memory_suggestions
from backend.pipeline.patch_failures import (
    PatchFailureType,
    build_patch_failure_report,
    patch_failure_report_to_completion_summary,
)

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


def _success_summary(entries: list[str]) -> dict:
    return {
        "chunk_title": "Chunk",
        "summary": "Goal: x\nCoder summary: y",
        "suggested_memory_entries": entries,
    }


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


def test_similar_candidate_from_different_run_still_generates(run_env):
    project_id = run_env.create_project()
    handoff = "Repo indexing uses project_id isolation everywhere."

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

    # A *different* run proposing the same content is still allowed: run-scoped
    # suppression keys on source_run_id, and no pending/active copy exists.
    run_two = run_env.create_run(project_id, status="complete")
    run_env.add_chunk(
        run_two, project_id, 1,
        completion_summary=_success_summary([handoff]),
    )
    second = generate_run_memory_suggestions(run_two)

    regenerated = [s for s in second.generated if s["content"] == handoff]
    assert len(regenerated) == 1
    assert regenerated[0]["source_run_id"] == run_two
    assert regenerated[0]["status"] == "pending"


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
    all_text = " ".join(
        s["content"] for s in list_suggestions(project_id, status="pending")
    ).lower()
    assert "skip approval" not in all_text
    assert "auto-merge" not in all_text
    # The unsafe handoff entries never became active memory facts.
    assert list_facts(project_id, status="active") == []


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
    assert all(s["status"] == "pending" for s in body["suggestions"])
    assert all(s["source_run_id"] == run_id for s in body["suggestions"])

    assert second.status_code == 200
    assert second.json()["generated_count"] == 0
    assert second.json()["skipped_count"] >= 1


def test_generate_endpoint_missing_run_returns_404(client, run_env):
    response = client.post(_generate_url("does-not-exist"))
    assert response.status_code == 404
