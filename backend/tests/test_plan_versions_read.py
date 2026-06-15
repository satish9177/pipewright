"""
Tests for the read-only plan-version lineage endpoint.

GET /runs/{run_id}/plan-versions composes append-only plan_versions rows with
sanitized plan-turn messages. It is an audit read model only: no capability flag,
approval state, execution state, live chunk plan, or existing /chunks response is
mutated by reading it.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.core.statuses import ChunkPlanStatus, RunStatus
from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy
from backend.pipeline.chunk_store import create_chunked_run
from backend.pipeline.plan_version_store import (
    PLAN_VERSION_SOURCE_INITIAL,
    PLAN_VERSION_SOURCE_PLAN_TURN,
    append_plan_version,
)
from backend.pipeline.run_turn_store import record_plan_turn
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
        name=f"Plan Version Read Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _chunk(number: int, title: str) -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=number,
        title=title,
        description=f"Do {title}.",
        files_expected=[f"src/{title.lower().replace(' ', '_')}.py"],
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


def _create_awaiting_run(
    tmp_repo,
    tracked_runs,
    titles: list[str] | None = None,
):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Update the dashboard",
        _triage(
            run_id,
            project["id"],
            "Update the dashboard",
            titles or ["Initial chunk"],
        ),
    )
    return run_id, project


def _insert_run_without_plan_versions(tmp_repo, tracked_runs) -> str:
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO pipeline_runs (
                    id,
                    project_id,
                    feature_description,
                    status,
                    current_step,
                    intent,
                    chunk_plan_status,
                    total_chunks,
                    current_chunk_number
                )
                VALUES (
                    :id,
                    :project_id,
                    'Legacy run without plan versions',
                    'running',
                    'running',
                    'implementation',
                    'none',
                    0,
                    0
                )
            """),
            {"id": run_id, "project_id": project["id"]},
        )
    return run_id


def _append_plan_turn_version(
    run_id: str,
    project_id: str,
    *,
    version: int,
    message: str,
    titles: list[str],
) -> dict:
    turn = record_plan_turn(run_id=run_id, project_id=project_id, message=message)
    triage_json = _triage(
        run_id,
        project_id,
        "Update the dashboard",
        titles,
    ).model_dump_json()
    with engine.begin() as conn:
        append_plan_version(
            conn,
            run_id=run_id,
            version=version,
            triage_json=triage_json,
            source=PLAN_VERSION_SOURCE_PLAN_TURN,
            created_from_turn_id=turn["id"],
        )
    return turn


def _load_run_fields(run_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT status, chunk_plan_status
                FROM pipeline_runs
                WHERE id = :run_id
            """),
            {"run_id": run_id},
        ).mappings().fetchone()
    return dict(row) if row is not None else {}


def _row_count(table: str, run_id: str) -> int:
    run_column = "id" if table == "pipeline_runs" else "run_id"
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {run_column} = :run_id"),
            {"run_id": run_id},
        ).fetchone()
    return int(row[0])


def test_unknown_run_returns_404():
    client = TestClient(app)

    response = client.get(f"/runs/{uuid.uuid4()}/plan-versions")

    assert response.status_code == 404


def test_existing_run_with_zero_plan_versions_returns_empty_versions(
    tmp_repo,
    tracked_runs,
):
    run_id = _insert_run_without_plan_versions(tmp_repo, tracked_runs)
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "versions": []}


def test_v1_only_lineage_returns_initial_entry_without_triage_json(
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["versions"] == [
        {
            "version": 1,
            "source": PLAN_VERSION_SOURCE_INITIAL,
            "created_at": payload["versions"][0]["created_at"],
            "created_from_turn": None,
        }
    ]
    assert payload["versions"][0]["created_at"]
    assert "triage_json" not in payload["versions"][0]


def test_plan_turn_versions_are_ordered_and_link_turn_metadata(
    tmp_repo,
    tracked_runs,
):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    first_turn = _append_plan_turn_version(
        run_id,
        project["id"],
        version=2,
        message="Split the backend and frontend chunks.",
        titles=["Backend chunk", "Frontend chunk"],
    )
    second_turn = _append_plan_turn_version(
        run_id,
        project["id"],
        version=3,
        message="Add a final review chunk.",
        titles=["Backend chunk", "Frontend chunk", "Review chunk"],
    )
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    versions = response.json()["versions"]
    assert [version["version"] for version in versions] == [1, 2, 3]
    assert versions[0]["created_from_turn"] is None
    assert versions[1]["created_from_turn"] == {
        "turn_number": first_turn["turn_number"],
        "created_at": first_turn["created_at"],
        "message": first_turn["steer_text"],
    }
    assert versions[2]["created_from_turn"] == {
        "turn_number": second_turn["turn_number"],
        "created_at": second_turn["created_at"],
        "message": second_turn["steer_text"],
    }


def test_plan_turn_message_is_sanitized_in_lineage_response(
    tmp_repo,
    tracked_runs,
):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    raw_secret = "a" * 40
    turn = _append_plan_turn_version(
        run_id,
        project["id"],
        version=2,
        message=f"Use token={raw_secret} in the plan.",
        titles=["Backend chunk", "Frontend chunk"],
    )
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    assert raw_secret not in response.text
    assert response.json()["versions"][1]["created_from_turn"]["message"] == (
        turn["steer_text"]
    )
    assert "[REDACTED]" in response.json()["versions"][1]["created_from_turn"][
        "message"
    ]


def test_dangling_created_from_turn_id_degrades_to_null(tmp_repo, tracked_runs):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    with engine.begin() as conn:
        append_plan_version(
            conn,
            run_id=run_id,
            version=2,
            triage_json=_triage(
                run_id,
                project["id"],
                "Update the dashboard",
                ["Backend chunk", "Frontend chunk"],
            ).model_dump_json(),
            source=PLAN_VERSION_SOURCE_PLAN_TURN,
            created_from_turn_id=str(uuid.uuid4()),
        )
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    assert response.json()["versions"][1]["created_from_turn"] is None


def test_flag_off_still_allows_plan_versions_read(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    monkeypatch.setattr(policy, "PLAN_TURNS_ENABLED", False)
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    assert response.json()["versions"][0]["version"] == 1


def test_plan_versions_read_is_read_only(tmp_repo, tracked_runs):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    before_run = _load_run_fields(run_id)
    before_counts = {
        table: _row_count(table, run_id)
        for table in ("pipeline_runs", "plan_versions", "run_turns", "chunks")
    }
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/plan-versions")

    assert response.status_code == 200
    assert _load_run_fields(run_id) == before_run
    assert before_run == {
        "status": RunStatus.AWAITING_CHUNK_PLAN_APPROVAL,
        "chunk_plan_status": ChunkPlanStatus.AWAITING_APPROVAL,
    }
    assert {
        table: _row_count(table, run_id)
        for table in ("pipeline_runs", "plan_versions", "run_turns", "chunks")
    } == before_counts


def test_get_chunks_response_is_unchanged_by_plan_versions_read(
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    client = TestClient(app)
    before = client.get(f"/runs/{run_id}/chunks")

    lineage = client.get(f"/runs/{run_id}/plan-versions")
    after = client.get(f"/runs/{run_id}/chunks")

    assert before.status_code == 200
    assert lineage.status_code == 200
    assert after.status_code == 200
    assert after.json() == before.json()
