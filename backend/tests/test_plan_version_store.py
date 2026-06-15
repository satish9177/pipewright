"""
Tests for the dormant §23 row 7a plan_versions scaffold.

The table is append-only infrastructure only: runtime still reads
pipeline_runs.chunk_plan, and no approval/version binding is introduced here.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from backend.db import database
from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import plan_version_store
from backend.pipeline.chunk_store import create_chunked_run
from backend.pipeline.plan_version_store import (
    PLAN_VERSION_SOURCE_INITIAL,
    PLAN_VERSION_SOURCE_PLAN_TURN,
    PLAN_VERSION_SOURCE_SEEDED,
    append_plan_version,
    list_plan_versions,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in ("plan_versions", "chunks"):
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
        name=f"Plan Version Project {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )


def _make_triage(
    run_id: str,
    project_id: str,
    feature: str = "Add login",
) -> TriageResult:
    return TriageResult(
        run_id=run_id,
        project_id=project_id,
        feature_description=feature,
        complexity="easy",
        total_chunks=1,
        reasoning="Single chunk plan.",
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="Login chunk",
                description="Implement login.",
                files_expected=["backend/routes/login.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=200,
                requires_human_review=False,
                rationale="Small focused change.",
            )
        ],
    )


def _insert_plan_ready_run(
    run_id: str,
    project_id: str,
    feature: str = "Add login",
    chunk_plan: str | None = None,
) -> None:
    if chunk_plan is None:
        chunk_plan = _make_triage(run_id, project_id, feature).model_dump_json()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO pipeline_runs
                (
                    id, project_id, feature_description, status, current_step,
                    intent, chunk_plan_status, chunk_plan,
                    total_chunks, current_chunk_number
                )
                VALUES
                (
                    :id, :project_id, :feature, 'plan_ready', 'plan_ready',
                    'plan_only', 'none', :chunk_plan, 1, 0
                )
            """),
            {
                "id": run_id,
                "project_id": project_id,
                "feature": feature,
                "chunk_plan": chunk_plan,
            },
        )


def _load_run(run_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM pipeline_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).fetchone()
    return dict(row._mapping) if row is not None else {}


def test_implementation_run_writes_initial_v1_plan_version(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = _make_triage(run_id, project["id"])

    create_chunked_run(run_id, project["id"], "Add login", triage)

    versions = list_plan_versions(run_id)
    assert len(versions) == 1
    version = versions[0]
    run = _load_run(run_id)
    assert version["version"] == 1
    assert version["source"] == PLAN_VERSION_SOURCE_INITIAL
    assert version["created_from_turn_id"] is None
    assert version["triage_json"] == run["chunk_plan"]
    assert json.loads(version["triage_json"]) == json.loads(triage.model_dump_json())


def test_plan_only_creation_writes_initial_v1_plan_version(tracked_runs):
    run_id = str(uuid.uuid4())
    project_id = f"project-{uuid.uuid4()}"
    tracked_runs.append(run_id)
    triage_json = _make_triage(run_id, project_id).model_dump_json()

    from backend.routes.chunks import _create_read_only_run

    _create_read_only_run(
        run_id=run_id,
        project_id=project_id,
        feature_description="Add login",
        intent="plan_only",
        status="plan_ready",
        summary="Single chunk plan.",
        chunk_plan=triage_json,
        total_chunks=1,
    )

    versions = list_plan_versions(run_id)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["source"] == PLAN_VERSION_SOURCE_INITIAL
    assert versions[0]["created_from_turn_id"] is None
    assert versions[0]["triage_json"] == _load_run(run_id)["chunk_plan"]


def test_plan_handoff_writes_seeded_v1_for_implementation_run(
    tmp_repo,
    tracked_runs,
):
    project = _make_project(tmp_repo)
    plan_run_id = str(uuid.uuid4())
    tracked_runs.append(plan_run_id)
    feature = "Add password reset"
    _insert_plan_ready_run(plan_run_id, project["id"], feature)
    before = _load_run(plan_run_id)

    response = TestClient(app).post(f"/runs/{plan_run_id}/start-implementation")

    assert response.status_code == 200
    new_run_id = response.json()["run_id"]
    tracked_runs.append(new_run_id)
    after = _load_run(plan_run_id)
    for field in (
        "status",
        "current_step",
        "intent",
        "chunk_plan_status",
        "chunk_plan",
        "feature_description",
        "project_id",
    ):
        assert before[field] == after[field], f"{field} mutated on source"

    versions = list_plan_versions(new_run_id)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["source"] == PLAN_VERSION_SOURCE_SEEDED
    assert versions[0]["created_from_turn_id"] is None
    assert versions[0]["triage_json"] == _load_run(new_run_id)["chunk_plan"]
    assert json.loads(versions[0]["triage_json"])["run_id"] == new_run_id


def test_duplicate_run_version_is_rejected(tmp_repo, tracked_runs):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = _make_triage(run_id, project["id"])
    create_chunked_run(run_id, project["id"], "Add login", triage)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            append_plan_version(
                conn,
                run_id=run_id,
                version=1,
                triage_json=triage.model_dump_json(),
                source=PLAN_VERSION_SOURCE_INITIAL,
            )

    assert len(list_plan_versions(run_id)) == 1


def test_plan_turn_source_is_allowed(tracked_runs):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)

    with engine.begin() as conn:
        append_plan_version(
            conn,
            run_id=run_id,
            version=1,
            triage_json=_make_triage(run_id, "project-plan-turn").model_dump_json(),
            source=PLAN_VERSION_SOURCE_PLAN_TURN,
            created_from_turn_id="turn-1",
        )

    versions = list_plan_versions(run_id)
    assert len(versions) == 1
    assert versions[0]["source"] == PLAN_VERSION_SOURCE_PLAN_TURN
    assert versions[0]["created_from_turn_id"] == "turn-1"


def test_plan_version_store_has_no_update_or_delete_api():
    public_names = {
        name for name in dir(plan_version_store) if not name.startswith("_")
    }

    assert "append_plan_version" in public_names
    assert "list_plan_versions" in public_names
    assert all("update" not in name for name in public_names)
    assert all("delete" not in name for name in public_names)


def test_plan_version_write_is_atomic_with_run_creation(
    monkeypatch,
    tmp_repo,
    tracked_runs,
):
    project = _make_project(tmp_repo)
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = _make_triage(run_id, project["id"])

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("forced plan version failure")

    monkeypatch.setattr(
        "backend.pipeline.chunk_store.append_plan_version",
        fail_append,
    )

    with pytest.raises(RuntimeError, match="create_chunked_run failed"):
        create_chunked_run(run_id, project["id"], "Add login", triage)

    with engine.connect() as conn:
        run_count = conn.execute(
            text("SELECT COUNT(*) FROM pipeline_runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).fetchone()[0]
        chunk_count = conn.execute(
            text("SELECT COUNT(*) FROM chunks WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).fetchone()[0]
        version_count = conn.execute(
            text("SELECT COUNT(*) FROM plan_versions WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).fetchone()[0]

    assert (run_count, chunk_count, version_count) == (0, 0, 0)


def test_plan_versions_migration_shape_is_idempotent():
    legacy_engine = create_engine("sqlite:///:memory:")
    with legacy_engine.begin() as conn:
        database._ensure_plan_versions_shape(conn)
        database._ensure_plan_versions_shape(conn)
        columns = [
            row._mapping["name"]
            for row in conn.execute(text("PRAGMA table_info(plan_versions)"))
        ]
        indexes = {
            row._mapping["name"]: row._mapping["unique"]
            for row in conn.execute(text("PRAGMA index_list(plan_versions)"))
        }

    assert columns == [
        "id",
        "run_id",
        "version",
        "triage_json",
        "source",
        "created_from_turn_id",
        "created_at",
    ]
    assert indexes["idx_plan_versions_run_version"] == 1
    assert "idx_plan_versions_run" not in indexes


def test_plan_versions_migration_expands_legacy_source_check_and_preserves_rows():
    legacy_engine = create_engine("sqlite:///:memory:")
    run_id = str(uuid.uuid4())
    with legacy_engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE plan_versions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                triage_json TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('initial', 'seeded')),
                created_from_turn_id TEXT,
                created_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX idx_plan_versions_run_version
            ON plan_versions(run_id, version)
        """))
        conn.execute(text("""
            INSERT INTO plan_versions (
                id, run_id, version, triage_json, source, created_at
            )
            VALUES (
                'pv-1', :run_id, 1, '{"plan": 1}', 'initial',
                '2026-06-15T00:00:00+00:00'
            )
        """), {"run_id": run_id})

        database._ensure_plan_versions_shape(conn)
        source_sql = conn.execute(text("""
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'plan_versions'
        """)).fetchone()[0]
        rows = conn.execute(text("""
            SELECT id, run_id, version, triage_json, source, created_from_turn_id
            FROM plan_versions
            ORDER BY version
        """)).fetchall()
        conn.execute(text("""
            INSERT INTO plan_versions (
                id, run_id, version, triage_json, source,
                created_from_turn_id, created_at
            )
            VALUES (
                'pv-2', :run_id, 2, '{"plan": 2}', 'plan_turn',
                'turn-1', '2026-06-15T00:00:01+00:00'
            )
        """), {"run_id": run_id})

    assert "plan_turn" in source_sql
    assert rows == [
        (
            "pv-1",
            run_id,
            1,
            '{"plan": 1}',
            "initial",
            None,
        )
    ]
