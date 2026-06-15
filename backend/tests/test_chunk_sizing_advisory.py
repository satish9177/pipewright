import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import policy
from backend.pipeline.chunk_sizing import (
    SIZE_NOTE_PREFIX,
    annotate_triage_result_sizing,
)
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def sizing_project():
    project_id = f"size-{uuid.uuid4()}"
    run_ids: list[str] = []
    yield project_id, run_ids
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
        conn.execute(
            text("DELETE FROM file_index WHERE project_id = :project_id"),
            {"project_id": project_id},
        )


def _seed_token_estimates(project_id: str, estimates: dict[str, int]) -> None:
    with engine.begin() as conn:
        for path, estimate in estimates.items():
            conn.execute(text("""
                INSERT INTO file_index
                (id, project_id, path, file_type, summary, key_imports,
                 last_modified, token_estimate, line_count, size_bytes)
                VALUES
                (:id, :project_id, :path, 'unknown', NULL, '[]',
                 NULL, :token_estimate, 10, 100)
            """), {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "path": path,
                "token_estimate": estimate,
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
        feature_description="Adjust small files.",
        complexity="easy",
        total_chunks=len(chunks),
        chunks=chunks,
        reasoning="Chunk sizing advisory test.",
    )


def _enable_sizing(monkeypatch) -> None:
    monkeypatch.setattr(policy, "CHUNK_SIZING_ADVISORY_ENABLED", True)


def test_flag_off_passthrough_has_no_size_notes(monkeypatch, sizing_project):
    project_id, _ = sizing_project
    monkeypatch.setattr(policy, "CHUNK_SIZING_ADVISORY_ENABLED", False)
    _seed_token_estimates(project_id, {"docs/large.md": 10_000})
    triage = _triage(project_id, [_chunk(1, ["docs/large.md"])])

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )

    assert result is triage
    assert SIZE_NOTE_PREFIX not in result.chunks[0].rationale


def test_oversized_chunk_adds_size_note(monkeypatch, sizing_project):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    _seed_token_estimates(project_id, {"docs/large.md": 600})
    triage = _triage(project_id, [_chunk(1, ["docs/large.md"])])

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )

    assert SIZE_NOTE_PREFIX in result.chunks[0].rationale
    assert "600 tokens" in result.chunks[0].rationale
    assert "500 tokens" in result.chunks[0].rationale


def test_not_oversized_chunk_gets_no_oversized_note(monkeypatch, sizing_project):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    _seed_token_estimates(project_id, {"docs/right-sized.md": 500})
    triage = _triage(project_id, [_chunk(1, ["docs/right-sized.md"])])

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )

    assert SIZE_NOTE_PREFIX not in result.chunks[0].rationale


def test_missing_file_uses_new_file_default_tokens(monkeypatch, sizing_project):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    triage = _triage(project_id, [_chunk(1, ["docs/new-file.md"])])

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )

    assert SIZE_NOTE_PREFIX in result.chunks[0].rationale
    assert "800 tokens" in result.chunks[0].rationale
    assert "docs/new-file.md" in result.chunks[0].rationale


def test_many_files_adds_size_note(monkeypatch, sizing_project):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    paths = [f"docs/file-{index}.md" for index in range(7)]
    _seed_token_estimates(project_id, {path: 100 for path in paths})
    triage = _triage(project_id, [_chunk(1, paths)])

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=10_000,
    )

    assert SIZE_NOTE_PREFIX in result.chunks[0].rationale
    assert "touches 7 files" in result.chunks[0].rationale


def test_tiny_note_only_for_multi_chunk_non_empty_chunks(
    monkeypatch,
    sizing_project,
):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    _seed_token_estimates(project_id, {
        "docs/a.md": 100,
        "docs/b.md": 100,
    })

    multi = _triage(project_id, [
        _chunk(1, ["docs/a.md"]),
        _chunk(2, ["docs/b.md"]),
    ])
    single = _triage(project_id, [_chunk(1, ["docs/a.md"])])
    empty = _triage(project_id, [
        _chunk(1, []),
        _chunk(2, ["docs/b.md"]),
    ])

    multi_result = annotate_triage_result_sizing(
        project_id,
        multi,
        coder_context_window=10_000,
    )
    single_result = annotate_triage_result_sizing(
        project_id,
        single,
        coder_context_window=10_000,
    )
    empty_result = annotate_triage_result_sizing(
        project_id,
        empty,
        coder_context_window=10_000,
    )

    assert "very small" in multi_result.chunks[0].rationale
    assert SIZE_NOTE_PREFIX not in single_result.chunks[0].rationale
    assert SIZE_NOTE_PREFIX not in empty_result.chunks[0].rationale


def test_advisory_only_invariant_changes_only_rationale(
    monkeypatch,
    sizing_project,
):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    _seed_token_estimates(project_id, {"docs/large.md": 600})
    triage = _triage(project_id, [_chunk(1, ["docs/large.md"])])
    original = triage.chunks[0].model_dump()

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )

    updated = result.chunks[0].model_dump()
    original_rationale = original.pop("rationale")
    updated_rationale = updated.pop("rationale")
    assert updated == original
    assert updated_rationale != original_rationale
    assert SIZE_NOTE_PREFIX in updated_rationale


def test_idempotency_does_not_double_append_size_notes(
    monkeypatch,
    sizing_project,
):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    _seed_token_estimates(project_id, {"docs/large.md": 600})
    triage = _triage(project_id, [_chunk(1, ["docs/large.md"])])

    once = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )
    twice = annotate_triage_result_sizing(
        project_id,
        once,
        coder_context_window=1_000,
    )

    assert once.chunks[0].rationale == twice.chunks[0].rationale
    assert twice.chunks[0].rationale.count(SIZE_NOTE_PREFIX) == 1


def test_multiple_notes_are_each_prefixed(monkeypatch, sizing_project):
    project_id, _ = sizing_project
    _enable_sizing(monkeypatch)
    paths = [f"docs/file-{index}.md" for index in range(7)]
    _seed_token_estimates(project_id, {path: 100 for path in paths})
    triage = _triage(project_id, [_chunk(1, paths)])

    result = annotate_triage_result_sizing(
        project_id,
        triage,
        coder_context_window=1_000,
    )

    assert result.chunks[0].rationale.count(SIZE_NOTE_PREFIX) == 2
    assert "above the advisory chunk budget" in result.chunks[0].rationale
    assert "above the advisory threshold" in result.chunks[0].rationale


def test_route_persists_size_note_without_changing_risk_or_review(
    monkeypatch,
    tmp_repo,
    sizing_project,
):
    _project_id, run_ids = sizing_project
    _enable_sizing(monkeypatch)
    project = create_project(
        name=f"Sizing route {uuid.uuid4()}",
        repo_path=str(tmp_repo),
        test_command="python --version",
    )
    _seed_token_estimates(project["id"], {"docs/huge.md": 999_999})

    async def fake_triage(run_id, project_id, feature_description):
        return _triage(
            project_id,
            [_chunk(1, ["docs/huge.md"])],
            run_id=run_id,
        )

    monkeypatch.setattr("backend.routes.chunks.run_triage", fake_triage)
    client = TestClient(app)

    response = client.post("/runs/chunked", json={
        "project_id": project["id"],
        "feature_description": "Update docs/huge.md",
    })

    assert response.status_code == 200
    data = response.json()
    run_ids.append(data["run_id"])
    response_chunk = data["triage"]["chunks"][0]
    assert SIZE_NOTE_PREFIX in response_chunk["rationale"]
    assert response_chunk["risk_level"] == "low"
    assert response_chunk["requires_human_review"] is False

    read_response = client.get(f"/runs/{data['run_id']}/chunks")
    assert read_response.status_code == 200
    read_chunk = read_response.json()["triage"]["chunks"][0]
    assert SIZE_NOTE_PREFIX in read_chunk["rationale"]
    assert read_chunk["risk_level"] == "low"
    assert read_chunk["requires_human_review"] is False
