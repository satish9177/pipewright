"""
test_chunk_test_run_verdict_persistence.py
Tests for the display-only runtime test-validation verdict persistence (#28D).

These prove the verdict computed by the pure ``classify_test_run`` classifier is
recorded on the chunk and can be read back, WITHOUT touching chunk status, run
outcome, approval gates, or pass/fail. Persistence is evidence only.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.chunk_store import (
    create_chunked_run,
    get_chunk,
    get_chunk_test_run_verdict,
    save_chunk_test_run_verdict,
)
from backend.pipeline.test_run_validation import classify_test_run
from backend.projects.project_store import create_project

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(
                text("DELETE FROM chunks WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


def _make_run_with_one_chunk(tmp_path, tracked_runs, *, test_command="pytest"):
    project = create_project(
        name=f"verdict-{uuid.uuid4()}",
        repo_path=str(tmp_path),
        test_command=test_command,
    )
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    triage = TriageResult(
        run_id=run_id,
        project_id=project["id"],
        feature_description="x",
        complexity="easy",
        total_chunks=1,
        chunks=[
            ChunkDefinition(
                chunk_number=1,
                title="Chunk 1",
                description="do it",
                files_expected=["a.py"],
                depends_on=[],
                risk_level="low",
                token_estimate=10,
                requires_human_review=False,
                rationale="r",
            )
        ],
        reasoning="r",
    )
    create_chunked_run(run_id, project["id"], "x", triage)
    return run_id, project


def _save(run_id, command, exit_code, output):
    result = classify_test_run(command, exit_code, output)
    save_chunk_test_run_verdict(run_id, 1, result)
    return result


# --------------------------------------------------------------------------
# 1-5. Each verdict persists and reads back
# --------------------------------------------------------------------------


def test_strong_verdict_persists(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    _save(run_id, "pytest", 0, "===== 5 passed in 0.10s =====")

    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "strong"
    assert stored["command_quality"] == "likely_test"
    assert stored["passed_tests"] == 5
    assert stored["failed_tests"] is None
    assert stored["total_tests"] == 5
    assert stored["counts_parsed"] is True
    assert stored["zero_tests_detected"] is False


def test_weak_verdict_persists_for_version_probe(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(
        tmp_path, tracked_runs, test_command="python --version"
    )
    _save(run_id, "python --version", 0, "Python 3.11.5")

    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "weak"
    assert stored["command_quality"] == "weak"


def test_none_verdict_persists_for_blank_command(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    _save(run_id, "", 0, "")

    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "none"


def test_weak_verdict_persists_for_zero_tests(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    _save(run_id, "pytest", 0, "collected 0 items")

    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "weak"
    assert stored["zero_tests_detected"] is True


def test_unknown_verdict_persists_for_custom_script(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(
        tmp_path, tracked_runs, test_command="./scripts/check.sh"
    )
    _save(run_id, "./scripts/check.sh", 0, "running checks...")

    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "unknown"
    assert stored["command_quality"] == "unknown"


# --------------------------------------------------------------------------
# 6. Backward compatibility: a chunk with no recorded verdict reads as None
# --------------------------------------------------------------------------


def test_chunk_without_verdict_reads_none(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    assert get_chunk_test_run_verdict(run_id, 1) is None


def test_missing_chunk_reads_none(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    assert get_chunk_test_run_verdict(run_id, 999) is None


# --------------------------------------------------------------------------
# 7. Retry/recovery overwrites the verdict with the latest test run
# --------------------------------------------------------------------------


def test_latest_verdict_overwrites_previous(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    # First run looked weak (zero tests); a later corrected run is strong.
    _save(run_id, "pytest", 0, "collected 0 items")
    assert get_chunk_test_run_verdict(run_id, 1)["verdict"] == "weak"

    _save(run_id, "pytest", 0, "5 passed in 0.2s")
    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "strong"
    assert stored["passed_tests"] == 5
    assert stored["zero_tests_detected"] is False


# --------------------------------------------------------------------------
# 8-9. Counts persist; failure-path verdict is recorded but not "strong"
# --------------------------------------------------------------------------


def test_counts_persist_for_failure_path_verdict(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    # Non-zero exit on a recognized runner -> UNKNOWN (never strong), but parsed
    # counts are still recorded as evidence.
    _save(run_id, "pytest", 1, "1 failed, 4 passed in 1.0s")

    stored = get_chunk_test_run_verdict(run_id, 1)
    assert stored["verdict"] == "unknown"
    assert stored["verdict"] != "strong"
    assert stored["passed_tests"] == 4
    assert stored["failed_tests"] == 1
    assert stored["total_tests"] == 5
    assert stored["counts_parsed"] is True


# --------------------------------------------------------------------------
# 10. Persisting the verdict never mutates chunk status
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Schema/backward compatibility: the migration adds the nullable columns and
# DB init succeeds; pre-#28D chunks load fine (verdict columns just NULL).
# --------------------------------------------------------------------------


def test_migration_adds_nullable_verdict_columns():
    from backend.db.database import init_db

    init_db()  # idempotent; applies the #28D migration on existing local DBs
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(chunks)")).fetchall()
    columns = {row._mapping["name"]: row._mapping for row in rows}
    expected = [
        "test_run_verdict",
        "test_run_verdict_reason",
        "test_run_command_quality",
        "test_run_counts_parsed",
        "test_run_zero_tests_detected",
        "test_run_counts_json",
    ]
    for name in expected:
        assert name in columns, f"missing migrated column: {name}"
        # All nullable (notnull == 0) with no NOT NULL constraint, so existing
        # chunks load as NULL and are never gated.
        assert columns[name]["notnull"] == 0


def test_persisting_verdict_does_not_change_chunk_status(tmp_path, tracked_runs):
    run_id, _ = _make_run_with_one_chunk(tmp_path, tracked_runs)
    before = get_chunk(run_id, 1).status

    _save(run_id, "pytest", 0, "5 passed")
    _save(run_id, "python --version", 0, "")  # weak verdict
    _save(run_id, "pytest", 0, "collected 0 items")  # zero-test weak verdict

    after = get_chunk(run_id, 1).status
    assert before == "pending"
    assert after == "pending"  # unchanged by any verdict, including weak/none
