import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy
from backend.pipeline.chunk_isolation import (
    ISOLATION_NOTE_PREFIX,
    annotate_triage_result_isolation,
)
from backend.pipeline.chunk_sizing import SIZE_NOTE_PREFIX
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def isolation_project():
    project_id = f"isolation-{uuid.uuid4()}"
    run_ids: list[str] = []
    project_ids: list[str] = []
    yield project_id, run_ids, project_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(
                text("DELETE FROM approval_gates WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM chunks WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )
        all_project_ids = [project_id, *project_ids]
        for pid in all_project_ids:
            conn.execute(
                text("DELETE FROM file_index WHERE project_id = :project_id"),
                {"project_id": pid},
            )


def _seed_file_index(project_id: str, paths: list[str]) -> None:
    with engine.begin() as conn:
        for path in paths:
            conn.execute(text("""
                INSERT INTO file_index
                (id, project_id, path, file_type, summary, key_imports,
                 last_modified, token_estimate, line_count, size_bytes)
                VALUES
                (:id, :project_id, :path, 'unknown', NULL, '[]',
                 NULL, 100, 10, 100)
            """), {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "path": path,
            })


def _chunk(
    number: int,
    files_expected: list[str],
    *,
    rationale: str = "base rationale",
) -> ChunkDefinition:
    return ChunkDefinition(
        chunk_number=number,
        title=f"Chunk {number}",
        description=f"Adjust chunk {number}.",
        files_expected=files_expected,
        depends_on=list(range(1, number)),
        risk_level="low",
        token_estimate=100 + number,
        requires_human_review=False,
        rationale=rationale,
    )


def _triage(
    project_id: str,
    chunks: list[ChunkDefinition],
    *,
    run_id: str | None = None,
) -> TriageResult:
    return TriageResult(
        run_id=run_id or f"run-{uuid.uuid4()}",
        project_id=project_id,
        feature_description="Adjust isolated chunks.",
        complexity="easy",
        total_chunks=len(chunks),
        chunks=chunks,
        reasoning="Chunk isolation advisory test.",
    )


def _enable_isolation(monkeypatch) -> None:
    monkeypatch.setattr(policy, "CHUNK_ISOLATION_ADVISORY_ENABLED", True)


def test_flag_off_passthrough_has_no_isolation_notes(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    monkeypatch.setattr(policy, "CHUNK_ISOLATION_ADVISORY_ENABLED", False)
    triage = _triage(project_id, [
        _chunk(1, ["docs/shared.md"]),
        _chunk(2, ["docs/shared.md"]),
    ])

    result = annotate_triage_result_isolation(triage)

    assert result is triage
    assert all(
        ISOLATION_NOTE_PREFIX not in chunk.rationale
        for chunk in result.chunks
    )


def test_overlap_adds_note_to_each_sharing_chunk_and_skips_disjoint(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [
        _chunk(1, ["docs/shared.md"]),
        _chunk(2, ["./docs/shared.md"]),
        _chunk(3, ["docs/only-three.md"]),
    ])

    result = annotate_triage_result_isolation(triage)

    assert ISOLATION_NOTE_PREFIX in result.chunks[0].rationale
    assert "docs/shared.md" in result.chunks[0].rationale
    assert "chunk 2" in result.chunks[0].rationale
    assert ISOLATION_NOTE_PREFIX in result.chunks[1].rationale
    assert "chunk 1" in result.chunks[1].rationale
    assert ISOLATION_NOTE_PREFIX not in result.chunks[2].rationale


def test_migration_mixing_adds_note_but_migration_only_does_not(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [
        _chunk(1, ["backend/migrations/001_add_users.py", "backend/app.py"]),
        _chunk(2, ["backend/migrations/002_add_roles.py"]),
    ])

    result = annotate_triage_result_isolation(triage)

    assert ISOLATION_NOTE_PREFIX in result.chunks[0].rationale
    assert "mixes migration file(s)" in result.chunks[0].rationale
    assert "backend/migrations/001_add_users.py" in result.chunks[0].rationale
    assert "backend/app.py" in result.chunks[0].rationale
    assert ISOLATION_NOTE_PREFIX not in result.chunks[1].rationale


def test_model_paths_are_not_treated_as_migrations(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [_chunk(1, ["models/user.py", "backend/app.py"])])

    result = annotate_triage_result_isolation(triage)

    assert ISOLATION_NOTE_PREFIX not in result.chunks[0].rationale


def test_advisory_only_invariant_changes_only_rationale(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [
        _chunk(1, ["docs/shared.md"]),
        _chunk(2, ["docs/shared.md"]),
    ])
    original = triage.chunks[0].model_dump()

    result = annotate_triage_result_isolation(triage)

    updated = result.chunks[0].model_dump()
    original_rationale = original.pop("rationale")
    updated_rationale = updated.pop("rationale")
    assert updated == original
    assert updated_rationale != original_rationale
    assert ISOLATION_NOTE_PREFIX in updated_rationale


def test_idempotency_does_not_double_append_isolation_notes(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [
        _chunk(1, ["docs/shared.md"]),
        _chunk(2, ["docs/shared.md"]),
    ])

    once = annotate_triage_result_isolation(triage)
    twice = annotate_triage_result_isolation(once)

    assert once.chunks[0].rationale == twice.chunks[0].rationale
    assert twice.chunks[0].rationale.count(ISOLATION_NOTE_PREFIX) == 1


def test_multiple_notes_are_each_prefixed(monkeypatch, isolation_project):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [
        _chunk(1, ["backend/migrations/001_add_users.py", "docs/shared.md"]),
        _chunk(2, ["docs/shared.md"]),
    ])

    result = annotate_triage_result_isolation(triage)

    assert result.chunks[0].rationale.count(ISOLATION_NOTE_PREFIX) == 2
    assert "shares file(s)" in result.chunks[0].rationale
    assert "mixes migration file(s)" in result.chunks[0].rationale


def test_harmless_single_chunk_gets_no_note_or_hardening(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [_chunk(1, ["utils/calculator.py"])])

    result = annotate_triage_result_isolation(triage)

    chunk = result.chunks[0]
    assert ISOLATION_NOTE_PREFIX not in chunk.rationale
    assert chunk.risk_level == "low"
    assert chunk.requires_human_review is False


def test_combined_size_and_isolation_markers_remain_separate(
    monkeypatch,
    isolation_project,
):
    project_id, _, _ = isolation_project
    _enable_isolation(monkeypatch)
    triage = _triage(project_id, [
        _chunk(
            1,
            ["docs/shared.md"],
            rationale=f"base {SIZE_NOTE_PREFIX} size note",
        ),
        _chunk(2, ["docs/shared.md"]),
    ])

    result = annotate_triage_result_isolation(triage)

    assert SIZE_NOTE_PREFIX in result.chunks[0].rationale
    assert ISOLATION_NOTE_PREFIX in result.chunks[0].rationale
    assert result.chunks[0].rationale.index(SIZE_NOTE_PREFIX) < (
        result.chunks[0].rationale.index(ISOLATION_NOTE_PREFIX)
    )


def test_route_persists_isolation_note_without_changing_risk_or_review(
    monkeypatch,
    tmp_repo,
    isolation_project,
):
    _project_id, run_ids, project_ids = isolation_project
    _enable_isolation(monkeypatch)
    project = create_project(
        name=f"Isolation route {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )
    project_ids.append(project["id"])
    _seed_file_index(project["id"], ["docs/shared.md", "docs/other.md"])

    async def fake_triage(run_id, project_id, feature_description):
        return _triage(
            project_id,
            [
                _chunk(1, ["docs/shared.md"]),
                _chunk(2, ["docs/shared.md", "docs/other.md"]),
            ],
            run_id=run_id,
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "update docs/shared.md copy",
        "requested_mode": "implementation",
        "confirm_conflict": True,
    })

    assert response.status_code == 200
    data = response.json()
    run_ids.append(data["run_id"])
    response_chunk = data["triage"]["chunks"][0]
    assert ISOLATION_NOTE_PREFIX in response_chunk["rationale"]
    assert response_chunk["risk_level"] == "low"
    assert response_chunk["requires_human_review"] is False

    read_response = client.get(f"/runs/{data['run_id']}/chunks")
    assert read_response.status_code == 200
    read_chunk = read_response.json()["triage"]["chunks"][0]
    assert ISOLATION_NOTE_PREFIX in read_chunk["rationale"]
    assert read_chunk["risk_level"] == "low"
    assert read_chunk["requires_human_review"] is False
