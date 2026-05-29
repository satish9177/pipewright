"""
test_plan_path_grounding.py
Unit tests for deterministic repo-aware files_expected grounding (PR #9B).

No LLM, no filesystem walk. Tests seed a project's file_index directly.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.plan_path_grounding import (
    GROUNDING_UNCERTAINTY_NOTE,
    ground_chunk_files_expected,
    ground_triage_result_paths,
    is_grounded_path,
)

pytestmark = pytest.mark.unit


def _seed_index(project_id: str, paths: list[str]) -> None:
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


@pytest.fixture()
def indexed_project():
    """A project_id with a known, realistic indexed layout."""
    project_id = f"grounding-{uuid.uuid4()}"
    _seed_index(project_id, [
        "backend/routes/users.py",
        "backend/services/user_service.py",
        "frontend/src/App.tsx",
        "frontend/src/components/Header.tsx",
        "README.md",
    ])
    yield project_id
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM file_index WHERE project_id = :pid"),
            {"pid": project_id},
        )


def _chunk(files_expected: list[str], **overrides) -> ChunkDefinition:
    base = dict(
        chunk_number=1,
        title="Some chunk",
        description="Do a thing",
        files_expected=files_expected,
        depends_on=[],
        risk_level="low",
        token_estimate=100,
        requires_human_review=False,
        rationale="base rationale",
    )
    base.update(overrides)
    return ChunkDefinition(**base)


# --------------------------------------------------------------------------
# is_grounded_path
# --------------------------------------------------------------------------

def test_exact_indexed_path_is_grounded(indexed_project):
    assert is_grounded_path(indexed_project, "backend/routes/users.py") is True
    assert is_grounded_path(indexed_project, "README.md") is True


def test_new_file_under_indexed_dir_with_matching_ext_is_grounded(indexed_project):
    # backend/routes contains .py files; a new .py route is allowed.
    assert is_grounded_path(indexed_project, "backend/routes/health.py") is True
    # frontend/src contains .tsx; a new .tsx component is allowed.
    assert is_grounded_path(indexed_project, "frontend/src/LoginPage.tsx") is True


def test_fake_src_path_removed_when_src_not_indexed(indexed_project):
    assert is_grounded_path(indexed_project, "src/features/extra.py") is False


def test_fake_backend_src_path_removed_when_not_indexed(indexed_project):
    assert (
        is_grounded_path(indexed_project, "backend/src/services/auth_service.py")
        is False
    )


def test_fake_frontend_js_removed_when_area_uses_tsx(indexed_project):
    # frontend/src is a .tsx area; a .js file there is an extension conflict.
    assert is_grounded_path(indexed_project, "frontend/src/LoginPage.js") is False


def test_invented_root_file_not_grounded(indexed_project):
    # Root-level new files are never invented; only exact matches survive.
    assert is_grounded_path(indexed_project, "CONTRIBUTING.md") is False


def test_unsafe_paths_not_grounded(indexed_project):
    assert is_grounded_path(indexed_project, "../escape.py") is False
    assert is_grounded_path(indexed_project, "/etc/passwd") is False


def test_empty_index_grounds_nothing():
    empty_project = f"empty-{uuid.uuid4()}"
    assert is_grounded_path(empty_project, "backend/routes/users.py") is False


# --------------------------------------------------------------------------
# ground_chunk_files_expected
# --------------------------------------------------------------------------

def test_grounded_chunk_preserves_real_paths_unchanged(indexed_project):
    chunk = _chunk(["backend/routes/users.py", "frontend/src/App.tsx"])
    grounded = ground_chunk_files_expected(indexed_project, chunk)
    assert grounded.files_expected == [
        "backend/routes/users.py",
        "frontend/src/App.tsx",
    ]
    # No removal => no hardening, rationale untouched.
    assert grounded.risk_level == "low"
    assert grounded.requires_human_review is False
    assert grounded.rationale == "base rationale"


def test_removed_paths_force_high_risk_and_human_review(indexed_project):
    chunk = _chunk([
        "frontend/src/components/LoginPage.js",  # .js in tsx area -> removed
        "backend/src/routes/auth.py",            # backend/src not indexed -> removed
        "backend/routes/users.py",               # real -> kept
    ])
    grounded = ground_chunk_files_expected(indexed_project, chunk)
    assert grounded.files_expected == ["backend/routes/users.py"]
    assert grounded.risk_level == "high"
    assert grounded.requires_human_review is True


def test_rationale_includes_uncertainty_and_removed_paths(indexed_project):
    chunk = _chunk(["src/features/extra_ordinary_feature.py"])
    grounded = ground_chunk_files_expected(indexed_project, chunk)
    assert grounded.files_expected == []
    assert GROUNDING_UNCERTAINTY_NOTE.split(",")[0] in grounded.rationale
    assert "Removed ungrounded paths" in grounded.rationale
    assert "src/features/extra_ordinary_feature.py" in grounded.rationale


def test_empty_files_expected_stays_empty_no_invented_fallback(indexed_project):
    chunk = _chunk(["src/api/health.py", "src/app.py"])
    grounded = ground_chunk_files_expected(indexed_project, chunk)
    assert grounded.files_expected == []
    assert grounded.risk_level == "high"
    assert grounded.requires_human_review is True


def test_chunk_with_no_files_expected_is_left_untouched(indexed_project):
    chunk = _chunk([])
    grounded = ground_chunk_files_expected(indexed_project, chunk)
    # Nothing to remove -> no hardening here (the risk_scanner separately
    # forces empty files_expected to high risk downstream).
    assert grounded.files_expected == []
    assert grounded.rationale == "base rationale"


# --------------------------------------------------------------------------
# ground_triage_result_paths
# --------------------------------------------------------------------------

def test_ground_triage_result_grounds_every_chunk(indexed_project):
    triage = TriageResult(
        run_id="run-1",
        project_id=indexed_project,
        feature_description="add login",
        complexity="medium",
        total_chunks=2,
        reasoning="two chunks",
        chunks=[
            _chunk(["backend/routes/users.py"], chunk_number=1, title="Real"),
            _chunk(
                ["frontend/src/components/LoginPage.js"],
                chunk_number=2,
                title="Fake",
                depends_on=[1],
            ),
        ],
    )
    grounded = ground_triage_result_paths(indexed_project, triage)
    assert grounded.total_chunks == 2  # chunk count never changes
    assert grounded.chunks[0].files_expected == ["backend/routes/users.py"]
    assert grounded.chunks[0].risk_level == "low"
    assert grounded.chunks[1].files_expected == []
    assert grounded.chunks[1].risk_level == "high"
    assert grounded.chunks[1].requires_human_review is True
