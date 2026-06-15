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
from backend.pipeline import chunked_orchestrator
from backend.pipeline import policy
from backend.pipeline.chunk_store import (
    approve_chunk_plan,
    create_chunked_run,
    reject_chunk_plan,
)
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


def _load_approved_plan_version(run_id: str) -> int | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT approved_plan_version
                FROM pipeline_runs
                WHERE id = :run_id
            """),
            {"run_id": run_id},
        ).fetchone()
    return row[0] if row is not None else None


def _set_approved_plan_version(run_id: str, version: int | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pipeline_runs
                SET approved_plan_version = :version
                WHERE id = :run_id
            """),
            {"run_id": run_id, "version": version},
        )


def _delete_plan_versions(run_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM plan_versions WHERE run_id = :run_id"),
            {"run_id": run_id},
        )


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
    assert response.json() == {
        "run_id": run_id,
        "approved_version": None,
        "versions": [],
    }


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
    assert payload["approved_version"] is None
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


def test_v1_only_approval_stamps_approved_plan_version_and_read_model(
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    client = TestClient(app)

    approved = approve_chunk_plan(run_id)
    lineage = client.get(f"/runs/{run_id}/plan-versions")

    assert approved.chunk_plan_status == ChunkPlanStatus.APPROVED
    assert _load_approved_plan_version(run_id) == 1
    assert lineage.status_code == 200
    assert lineage.json()["approved_version"] == 1


def test_approval_after_plan_turn_versions_stamps_latest_version(
    tmp_repo,
    tracked_runs,
):
    run_id, project = _create_awaiting_run(tmp_repo, tracked_runs)
    _append_plan_turn_version(
        run_id,
        project["id"],
        version=2,
        message="Split backend and frontend chunks.",
        titles=["Backend chunk", "Frontend chunk"],
    )
    _append_plan_turn_version(
        run_id,
        project["id"],
        version=3,
        message="Add a review chunk.",
        titles=["Backend chunk", "Frontend chunk", "Review chunk"],
    )
    client = TestClient(app)

    approve_chunk_plan(run_id)
    response = client.get(f"/runs/{run_id}/plan-versions")

    assert _load_approved_plan_version(run_id) == 3
    assert response.status_code == 200
    assert response.json()["approved_version"] == 3


def test_legacy_zero_version_approval_succeeds_and_stamps_null(
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    _delete_plan_versions(run_id)
    client = TestClient(app)

    approved = approve_chunk_plan(run_id)
    response = client.get(f"/runs/{run_id}/plan-versions")

    assert approved.chunk_plan_status == ChunkPlanStatus.APPROVED
    assert _load_approved_plan_version(run_id) is None
    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "approved_version": None,
        "versions": [],
    }


def test_flag_off_approval_still_stamps_initial_version(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    monkeypatch.setattr(policy, "PLAN_TURNS_ENABLED", False)

    approve_chunk_plan(run_id)

    assert _load_approved_plan_version(run_id) == 1


def test_reject_path_leaves_approved_plan_version_null(tmp_repo, tracked_runs):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)

    rejected = reject_chunk_plan(run_id, "Needs a smaller plan.")

    assert rejected.chunk_plan_status == ChunkPlanStatus.REJECTED
    assert _load_approved_plan_version(run_id) is None


def test_reapprove_guard_does_not_rewrite_approved_plan_version(
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    approve_chunk_plan(run_id)

    with pytest.raises(RuntimeError, match="awaiting approval"):
        approve_chunk_plan(run_id)

    assert _load_approved_plan_version(run_id) == 1


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
    assert response.json()["approved_version"] is None
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


def test_get_chunks_response_does_not_expose_approved_plan_version(
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    approve_chunk_plan(run_id)
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/chunks")

    assert response.status_code == 200
    assert "approved_version" not in response.text
    assert "approved_plan_version" not in response.text


@pytest.mark.asyncio
async def test_execution_ignores_approved_plan_version(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    run_id, _project = _create_awaiting_run(tmp_repo, tracked_runs)
    approve_chunk_plan(run_id)
    _set_approved_plan_version(run_id, 999)
    captured: dict = {}

    class DummyRepoLock:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_execute_locked(run_id_arg, plan_status):
        captured["run_id"] = run_id_arg
        captured["plan_status"] = plan_status
        return {
            "status": "fake_execution_started",
            "total_chunks": plan_status.total_chunks,
        }

    monkeypatch.setattr(
        chunked_orchestrator,
        "project_repo_lock",
        lambda _project_id: DummyRepoLock(),
    )
    monkeypatch.setattr(
        chunked_orchestrator,
        "_execute_approved_chunks_locked",
        fake_execute_locked,
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result == {"status": "fake_execution_started", "total_chunks": 1}
    assert captured["run_id"] == run_id
    assert captured["plan_status"].chunk_plan_status == ChunkPlanStatus.APPROVED
    assert captured["plan_status"].triage.total_chunks == 1
