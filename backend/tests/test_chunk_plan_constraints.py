"""
Focused tests for F8 request/file-constraint surfacing on chunk-plan reads.

The constraints are display metadata only. scope_guard, approved files, approval
gates, execution, reviewer, commit, and rollback behavior stay unchanged.
"""

import inspect
import json
import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import approval_gate, chunk_driver, chunk_store, chunked_orchestrator
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    get_chunk_plan_status,
)
from backend.pipeline.file_scope_intent import extract_user_file_constraints
from backend.pipeline import scope_guard
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "approval_gates",
                "chunks",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


def _project(tmp_repo):
    return create_project(
        name=f"Constraints Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _triage(run_id: str, project_id: str) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description="Small change",
        complexity="easy",
        total_chunks=1,
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="Small change",
                description="Update the implementation.",
                files_expected=["src/app.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=100,
                requires_human_review=False,
                rationale="Small scoped edit.",
            )
        ],
        reasoning="One chunk.",
    )


def _create_plan(tmp_repo, tracked_runs, feature_description: str):
    project = _project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    return create_chunked_run(
        run_id,
        project["id"],
        feature_description,
        _triage(run_id, project["id"]),
    )


def test_only_modify_constraint_is_visible_in_plan_response(tmp_repo, tracked_runs):
    plan = _create_plan(
        tmp_repo,
        tracked_runs,
        "Only modify src/app.py",
    )

    constraints = plan.request_file_constraints
    assert constraints is not None
    assert constraints.hard_allowlist == ["src/app.py"]
    assert constraints.forbidden_files == []
    assert constraints.has_explicit_file_constraints is True


def test_do_not_touch_constraint_is_visible_in_plan_response(tmp_repo, tracked_runs):
    plan = _create_plan(
        tmp_repo,
        tracked_runs,
        "Do not touch backend/db/schema.sql",
    )

    constraints = plan.request_file_constraints
    assert constraints is not None
    assert constraints.forbidden_files == ["backend/db/schema.sql"]
    assert constraints.hard_allowlist == []
    assert constraints.has_explicit_file_constraints is True


def test_no_explicit_constraints_has_honest_empty_state(tmp_repo, tracked_runs):
    plan = _create_plan(
        tmp_repo,
        tracked_runs,
        "Create this feature but do not add tests",
    )

    constraints = plan.request_file_constraints
    assert constraints is not None
    assert constraints.has_explicit_file_constraints is False
    assert constraints.hard_allowlist == []
    assert constraints.forbidden_files == []
    assert constraints.reference_only_files == []
    assert constraints.uncertain_mentions == []
    assert constraints.empty_state == "No explicit file constraints detected."
    assert (
        constraints.concept_level_note
        == "Concept-level constraints may still need human review in the plan files below."
    )


def test_request_constraints_are_persisted_not_reparsed_on_read(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    plan = _create_plan(tmp_repo, tracked_runs, "Only modify src/app.py")

    def fail_parse(_text: str):
        raise AssertionError("stored constraints should be used")

    monkeypatch.setattr(chunk_store, "extract_user_file_constraints", fail_parse)

    reloaded = get_chunk_plan_status(plan.run_id)

    assert reloaded.request_file_constraints is not None
    assert reloaded.request_file_constraints.hard_allowlist == ["src/app.py"]


def test_legacy_missing_constraints_fallback_uses_empty_state(tmp_repo, tracked_runs):
    project = _project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Only modify src/app.py",
        _triage(run_id, project["id"]),
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET report_json = NULL WHERE id = :run_id"),
            {"run_id": run_id},
        )

    plan = get_chunk_plan_status(run_id)

    assert plan.request_file_constraints is not None
    assert plan.request_file_constraints.hard_allowlist == ["src/app.py"]


def test_existing_file_scope_parser_behavior_is_preserved():
    allow = extract_user_file_constraints("Only modify src/app.py")
    forbid = extract_user_file_constraints("Do not touch backend/db/schema.sql")
    reference = extract_user_file_constraints("Use src/app.py similar to README.md")
    uncertain = extract_user_file_constraints("src/app.py")

    assert allow.hard_allowlist == ("src/app.py",)
    assert forbid.forbidden_files == ("backend/db/schema.sql",)
    assert reference.reference_only_files == ("README.md",)
    assert uncertain.uncertain_mentions == ("src/app.py",)


def test_constraints_do_not_block_plan_approval(tmp_repo, tracked_runs):
    plan = _create_plan(
        tmp_repo,
        tracked_runs,
        "Do not touch backend/db/schema.sql",
    )

    approved = approve_chunk_plan(plan.run_id)

    assert approved.chunk_plan_status == "approved"
    assert approved.request_file_constraints is not None
    assert approved.request_file_constraints.forbidden_files == [
        "backend/db/schema.sql"
    ]


def test_request_constraints_are_not_read_as_authority():
    authority_sources = "\n".join(
        inspect.getsource(module)
        for module in (
            approval_gate,
            chunk_driver,
            chunked_orchestrator,
            scope_guard,
        )
    )

    assert "request_file_constraints" not in authority_sources
    assert "REQUEST_FILE_CONSTRAINTS_REPORT_KEY" not in authority_sources


def test_stored_constraints_shape_is_plain_audit_metadata(tmp_repo, tracked_runs):
    plan = _create_plan(tmp_repo, tracked_runs, "Only modify src/app.py")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT report_json FROM pipeline_runs WHERE id = :run_id"),
            {"run_id": plan.run_id},
        ).fetchone()

    assert row is not None
    report = json.loads(row[0])
    assert sorted(report) == ["request_file_constraints"]
    assert report["request_file_constraints"]["hard_allowlist"] == ["src/app.py"]
