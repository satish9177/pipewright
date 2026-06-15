"""
Tests for dormant plan-gate turn engine scaffolding (§23 row 7b / §19).

No HTTP route and no frontend contract are introduced here. The producer is an
internal backend seam that can revise only a run still awaiting plan approval.
"""

import json
import uuid

import pytest
from sqlalchemy import create_engine, text

from backend.core.statuses import ChunkPlanStatus, RunStatus
from backend.db import database
from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy
from backend.pipeline.chunk_store import create_chunked_run
from backend.pipeline.plan_turn_engine import (
    PlanTurnConflictError,
    PlanTurnLimitError,
    produce_next_plan_version,
)
from backend.pipeline.plan_version_store import (
    PLAN_VERSION_SOURCE_PLAN_TURN,
    list_plan_versions,
)
from backend.pipeline.run_turn_store import (
    PLAN_TURN_CHUNK_SENTINEL,
    RUN_TURN_TARGET_CHUNK,
    RUN_TURN_TARGET_PLAN,
    list_run_turns,
    record_plan_turn,
    record_run_turn,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in ("run_turns", "plan_versions", "chunks"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


def _make_project(tmp_repo):
    return create_project(
        name=f"Plan Turn Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _chunk(number: int, title: str) -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=number,
        title=title,
        description=f"Do {title}.",
        files_expected=[],
        depends_on=[] if number == 1 else [number - 1],
        risk_level="high",
        token_estimate=100 * number,
        requires_human_review=True,
        rationale="Exact files require human confirmation.",
    )


def _triage(
    run_id: str,
    project_id: str,
    feature: str,
    titles: list[str],
) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature,
        complexity="easy" if len(titles) == 1 else "medium",
        total_chunks=len(titles),
        reasoning="Test plan.",
        chunks=[_chunk(index, title) for index, title in enumerate(titles, start=1)],
    )


def _create_awaiting_run(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Update the dashboard",
        _triage(run_id, project["id"], "Update the dashboard", ["Initial chunk"]),
    )
    return run_id, project


def _load_run(run_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM pipeline_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().fetchone()
    return dict(row) if row is not None else {}


def _chunk_titles(run_id: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT title
            FROM chunks
            WHERE run_id = :run_id
            ORDER BY chunk_number
        """), {"run_id": run_id}).fetchall()
    return [row[0] for row in rows]


def _set_run_state(run_id: str, *, status: str, chunk_plan_status: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pipeline_runs
            SET status = :status,
                current_step = :status,
                chunk_plan_status = :chunk_plan_status
            WHERE id = :run_id
        """), {
            "run_id": run_id,
            "status": status,
            "chunk_plan_status": chunk_plan_status,
        })


def test_run_turns_migration_adds_chunk_target_type_to_existing_rows():
    legacy_engine = create_engine("sqlite:///:memory:")
    with legacy_engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE run_turns (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                chunk_number INTEGER NOT NULL,
                steer_text TEXT NOT NULL,
                attempt_id TEXT,
                outcome TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, turn_number)
            )
        """))
        conn.execute(text("""
            INSERT INTO run_turns (
                id, run_id, project_id, turn_number, chunk_number, steer_text
            )
            VALUES ('turn-1', 'run-1', 'project-1', 1, 7, 'fix it')
        """))

        database._ensure_run_turns_shape(conn)
        database._ensure_run_turns_shape(conn)
        rows = conn.execute(text("""
            SELECT target_type, chunk_number, steer_text
            FROM run_turns
            WHERE id = 'turn-1'
        """)).fetchall()
        columns = [
            row._mapping["name"]
            for row in conn.execute(text("PRAGMA table_info(run_turns)"))
        ]

    assert columns.count("target_type") == 1
    assert rows == [("chunk", 7, "fix it")]


def test_plan_and_chunk_turns_are_monotonic_across_target_types(tracked_runs):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)

    chunk_turn = record_run_turn(
        run_id=run_id,
        project_id="project-1",
        chunk_number=2,
        steer_text="Fix chunk two.",
    )
    plan_turn = record_plan_turn(
        run_id=run_id,
        project_id="project-1",
        message="Split the plan.",
    )
    second_chunk_turn = record_run_turn(
        run_id=run_id,
        project_id="project-1",
        chunk_number=1,
        steer_text="Try chunk one again.",
    )

    assert chunk_turn["target_type"] == RUN_TURN_TARGET_CHUNK
    assert plan_turn["target_type"] == RUN_TURN_TARGET_PLAN
    assert plan_turn["chunk_number"] == PLAN_TURN_CHUNK_SENTINEL
    assert second_chunk_turn["target_type"] == RUN_TURN_TARGET_CHUNK
    turns = list_run_turns(run_id)
    assert [turn["turn_number"] for turn in turns] == [1, 2, 3]
    assert [turn["target_type"] for turn in turns] == ["chunk", "plan", "chunk"]


@pytest.mark.asyncio
async def test_produce_next_plan_version_updates_live_plan_and_chunks(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    v1 = list_plan_versions(run_id)[0]
    original_plan_json = _load_run(run_id)["chunk_plan"]
    captured = {}

    async def fake_run_triage(run_id, project_id, feature_description):
        captured["feature_description"] = feature_description
        return _triage(
            run_id,
            project_id,
            "temporary revision context",
            ["API chunk", "UI chunk"],
        )

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fake_run_triage,
    )

    result = await produce_next_plan_version(
        run_id,
        "Split API and UI work into separate chunks.",
    )

    assert result.version == 2
    assert result.project_id == project["id"]
    versions = list_plan_versions(run_id)
    assert len(versions) == 2
    assert versions[0] == v1
    assert versions[1]["source"] == PLAN_VERSION_SOURCE_PLAN_TURN
    assert versions[1]["created_from_turn_id"] == result.turn_id

    run = _load_run(run_id)
    assert run["chunk_plan"] == versions[1]["triage_json"]
    assert run["chunk_plan"] != original_plan_json
    assert run["total_chunks"] == 2
    assert _chunk_titles(run_id) == ["API chunk", "UI chunk"]
    assert json.loads(run["chunk_plan"])["feature_description"] == (
        "Update the dashboard"
    )

    context = captured["feature_description"]
    assert "Update the dashboard" in context
    assert original_plan_json in context
    assert "Split API and UI work into separate chunks." in context
    assert "full replacement chunk plan, not a diff" in context

    plan_turns = list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)
    assert len(plan_turns) == 1
    assert plan_turns[0]["id"] == result.turn_id
    assert plan_turns[0]["chunk_number"] == PLAN_TURN_CHUNK_SENTINEL


@pytest.mark.asyncio
async def test_plan_turn_final_write_rolls_back_on_replace_failure(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    before_run = _load_run(run_id)
    before_chunks = _chunk_titles(run_id)
    before_versions = list_plan_versions(run_id)

    async def fake_run_triage(run_id, project_id, feature_description):
        return _triage(
            run_id,
            project_id,
            feature_description,
            ["Replacement chunk"],
        )

    def fail_replace(*_args, **_kwargs):
        raise RuntimeError("forced replacement failure")

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fake_run_triage,
    )
    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.replace_pending_chunks_for_plan",
        fail_replace,
    )

    with pytest.raises(RuntimeError, match="forced replacement failure"):
        await produce_next_plan_version(run_id, "Make the plan smaller.")

    assert _load_run(run_id)["chunk_plan"] == before_run["chunk_plan"]
    assert _load_run(run_id)["total_chunks"] == before_run["total_chunks"]
    assert _chunk_titles(run_id) == before_chunks
    assert list_plan_versions(run_id) == before_versions
    assert len(list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)) == 1


@pytest.mark.asyncio
async def test_status_change_during_retriage_discards_new_plan(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    before_run = _load_run(run_id)
    before_chunks = _chunk_titles(run_id)
    before_versions = list_plan_versions(run_id)

    async def fake_run_triage(run_id, project_id, feature_description):
        _set_run_state(
            run_id,
            status=RunStatus.CHUNK_PLAN_APPROVED,
            chunk_plan_status=ChunkPlanStatus.APPROVED,
        )
        return _triage(
            run_id,
            project_id,
            feature_description,
            ["Too late"],
        )

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fake_run_triage,
    )

    with pytest.raises(PlanTurnConflictError):
        await produce_next_plan_version(run_id, "Revise after approval.")

    assert _load_run(run_id)["chunk_plan"] == before_run["chunk_plan"]
    assert _load_run(run_id)["total_chunks"] == before_run["total_chunks"]
    assert _chunk_titles(run_id) == before_chunks
    assert list_plan_versions(run_id) == before_versions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "chunk_plan_status"),
    [
        (RunStatus.CHUNK_PLAN_APPROVED, ChunkPlanStatus.APPROVED),
        (RunStatus.REJECTED, ChunkPlanStatus.REJECTED),
        (RunStatus.RUNNING_CHUNKS, ChunkPlanStatus.APPROVED),
        (RunStatus.COMPLETE, ChunkPlanStatus.APPROVED),
        (RunStatus.PLAN_READY, ChunkPlanStatus.NONE),
        (RunStatus.REPORT_READY, ChunkPlanStatus.NONE),
    ],
)
async def test_wrong_status_cannot_produce_plan_turn(
    monkeypatch,
    tmp_repo,
    tracked_runs,
    status,
    chunk_plan_status,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    _set_run_state(run_id, status=status, chunk_plan_status=chunk_plan_status)

    async def fail_triage(*_args, **_kwargs):
        raise AssertionError("wrong-status plan turns must not run triage")

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fail_triage,
    )

    with pytest.raises(PlanTurnConflictError):
        await produce_next_plan_version(run_id, "Revise this plan.")

    assert list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN) == []
    assert len(list_plan_versions(run_id)) == 1


@pytest.mark.asyncio
async def test_plan_turn_cap_rejects_sixth_turn(monkeypatch, tmp_repo, tracked_runs):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    for index in range(policy.PLAN_TURN_CAP):
        record_plan_turn(
            run_id=run_id,
            project_id=project["id"],
            message=f"Plan turn {index + 1}",
        )

    async def fail_triage(*_args, **_kwargs):
        raise AssertionError("over-cap plan turn must not run triage")

    monkeypatch.setattr(
        "backend.pipeline.plan_turn_engine.run_triage",
        fail_triage,
    )

    with pytest.raises(PlanTurnLimitError):
        await produce_next_plan_version(run_id, "One more revision.")

    assert len(list_run_turns(run_id, target_type=RUN_TURN_TARGET_PLAN)) == (
        policy.PLAN_TURN_CAP
    )
    assert len(list_plan_versions(run_id)) == 1
