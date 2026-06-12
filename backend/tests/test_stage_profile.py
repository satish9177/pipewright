"""
Tests for Phase 4 item 17a trivial-task stage profile.

No live AI, no real GitHub, and no real repo mutation. The profile may only
replace the fresh planner LLM call with a deterministic PlannerHandoff for an
eligible sampled chunk; every downstream stage stays on the standard path.
"""

import inspect
import json
import uuid

import pytest
from sqlalchemy import create_engine, text

from backend.db import database
from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline import chunk_attempt_store, chunk_driver, chunked_orchestrator
from backend.pipeline.chunk_attempt_store import (
    get_latest_completed_attempt_head,
    list_chunk_attempts,
    record_chunk_attempt,
)
from backend.pipeline.chunk_store import get_chunk_plan_status
from backend.pipeline.chunk_store import approve_chunk_plan, create_chunked_run
from backend.pipeline.patch_failures import PatchFailureType
from backend.pipeline.stage_profile import (
    StageProfile,
    is_profile_denylisted,
    is_trivial_eligible,
    resolve_stage_profile,
    resolve_stage_profile_metadata,
    sampling_bucket,
    synthesize_trivial_plan,
)
from backend.tests.test_chunked_orchestrator import (
    _seed_failed_chunk,
    add_test_checkpoint,
    create_run,
    make_coder_result,
    make_project,
    make_triage,
    patch_git_preflight,
    patch_resume_git,
    patch_retry_pipeline,
    patch_success_pipeline,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in (
                "run_turns",
                "scope_expansion_requests",
                "chunk_attempts",
                "chunk_reviews",
                "approval_gates",
                "checkpoints",
                "chunks",
            ):
                conn.execute(text(f"DELETE FROM {table} WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM pipeline_runs WHERE id = :r"), {"r": run_id})


def _chunk(**overrides) -> ChunkDefinition:
    data = {
        "chunk_number": 1,
        "title": "Small edit",
        "description": "Change the existing implementation.",
        "files_expected": ["src/app.py"],
        "depends_on": [],
        "risk_level": "low",
        "token_estimate": 100,
        "requires_human_review": False,
        "rationale": "Tiny scoped edit",
    }
    data.update(overrides)
    return ChunkDefinition(**data)


def _triage(chunk: ChunkDefinition | None = None, **overrides) -> TriageResult:
    chunk = chunk or _chunk()
    data = {
        "run_id": "run-profile",
        "project_id": "project-profile",
        "feature_description": "Small edit",
        "complexity": "easy",
        "total_chunks": 1,
        "chunks": [chunk],
        "reasoning": "Trivial.",
    }
    data.update(overrides)
    return TriageResult(**data)


def _path_exists(_repo: str, rel_path: str) -> bool:
    return rel_path != "missing.py"


def _touch_expected_files(tmp_repo, run_id: str) -> None:
    plan = get_chunk_plan_status(run_id)
    assert plan.triage is not None
    for path in plan.triage.chunks[0].files_expected:
        target = tmp_repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("existing\n", encoding="utf-8")


def _chunk_read_model(run_id: str) -> dict:
    chunk = get_chunk_plan_status(run_id).chunks[0]
    return {
        "risk_level": chunk.risk_level,
        "requires_human_review": chunk.requires_human_review,
        "files_expected": list(chunk.files_expected),
    }


def _completion_summary(run_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT completion_summary
                FROM chunks
                WHERE run_id = :run_id AND chunk_number = 1
            """),
            {"run_id": run_id},
        ).fetchone()
    return json.loads(row[0])


def _final_approval_summary(run_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT plain_english_summary
                FROM approval_gates
                WHERE run_id = :run_id
                  AND approval_type = 'final'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"run_id": run_id},
        ).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    ("triage", "chunk", "exists", "expected"),
    [
        (None, _chunk(), _path_exists, False),
        (_triage(_chunk(), total_chunks=2, chunks=[_chunk(), _chunk(chunk_number=2)]), _chunk(), _path_exists, False),
        (_triage(complexity="medium"), _chunk(), _path_exists, False),
        (_triage(complexity="hard"), _chunk(), _path_exists, False),
        (_triage(_chunk(risk_level="medium")), _chunk(risk_level="medium"), _path_exists, False),
        (_triage(_chunk(risk_level="high", requires_human_review=True)), _chunk(risk_level="high", requires_human_review=True), _path_exists, False),
        (_triage(_chunk(requires_human_review=True)), _chunk(requires_human_review=True), _path_exists, False),
        (_triage(_chunk(files_expected=[])), _chunk(files_expected=[]), _path_exists, False),
        (_triage(_chunk(files_expected=["missing.py"])), _chunk(files_expected=["missing.py"]), _path_exists, False),
        (_triage(), _chunk(), _path_exists, True),
    ],
)
def test_trivial_eligibility_table(triage, chunk, exists, expected):
    assert (
        is_trivial_eligible(triage, chunk, "repo", path_exists=exists)
        is expected
    )


def test_trivial_eligibility_rejects_legacy_chunk_with_dependency():
    chunk = ChunkDefinition.model_construct(
        chunk_number=1,
        title="Legacy",
        description="Legacy malformed chunk",
        files_expected=["src/app.py"],
        depends_on=[1],
        risk_level="low",
        token_estimate=100,
        requires_human_review=False,
        rationale="legacy row",
    )
    triage = TriageResult.model_construct(
        run_id="run-profile",
        project_id="project-profile",
        feature_description="Small edit",
        complexity="easy",
        total_chunks=1,
        chunks=[chunk],
        reasoning="Legacy.",
    )

    assert is_trivial_eligible(triage, chunk, "repo", path_exists=_path_exists) is False


@pytest.mark.parametrize(
    "path",
    [
        "migrations/001_add_table.py",
        "backend/db/schema.sql",
        "src/auth/login.py",
        "src/auth.py",
        "login.py",
        "sessions.py",
        "jwt_utils.py",
        "oauth_client.py",
        "password_reset.py",
        "token_service.py",
        "crypto.py",
        "security.py",
        ".env.local",
        "config/api_secrets.py",
        "keys/private.pem",
        "requirements.txt",
        ".github/workflows/ci.yml",
        "vite.config.ts",
    ],
)
def test_trivial_eligibility_denylist_forces_standard(path):
    chunk = _chunk(files_expected=[path])
    assert is_profile_denylisted(path) is True
    assert (
        is_trivial_eligible(_triage(chunk), chunk, "repo", path_exists=_path_exists)
        is False
    )


def test_trivial_eligibility_defaults_to_standard_when_path_check_raises():
    def raises(_repo: str, _path: str) -> bool:
        raise RuntimeError("indeterminate")

    assert is_trivial_eligible(_triage(), _chunk(), "repo", path_exists=raises) is False


def test_sampling_is_stable_and_zero_is_hard_off():
    chunk = _chunk()
    triage = _triage(chunk)
    assert (
        resolve_stage_profile(
            triage,
            chunk,
            "missing-repo",
            run_id="run-any",
            sample_pct=0,
            path_exists=lambda *_: (_ for _ in ()).throw(RuntimeError("must not run")),
        )
        is StageProfile.STANDARD
    )
    off_resolution = resolve_stage_profile_metadata(
        triage,
        chunk,
        "missing-repo",
        run_id="run-any",
        sample_pct=0,
        path_exists=lambda *_: (_ for _ in ()).throw(RuntimeError("must not run")),
    )
    assert off_resolution.stage_profile is StageProfile.STANDARD
    assert off_resolution.trivial_profile_eligible is None

    assert sampling_bucket("stable-run", 1) == sampling_bucket("stable-run", 1)
    sampled_run = next(
        f"sampled-{i}" for i in range(1000) if sampling_bucket(f"sampled-{i}", 1) < 50
    )
    control_run = next(
        f"control-{i}" for i in range(1000) if sampling_bucket(f"control-{i}", 1) >= 50
    )
    assert (
        resolve_stage_profile(
            triage,
            chunk,
            "repo",
            run_id=sampled_run,
            sample_pct=50,
            path_exists=_path_exists,
        )
        is StageProfile.MERGED_PLAN_CODE
    )
    sampled_resolution = resolve_stage_profile_metadata(
        triage,
        chunk,
        "repo",
        run_id=sampled_run,
        sample_pct=50,
        path_exists=_path_exists,
    )
    assert sampled_resolution.stage_profile is StageProfile.MERGED_PLAN_CODE
    assert sampled_resolution.trivial_profile_eligible is True
    assert (
        resolve_stage_profile(
            triage,
            chunk,
            "repo",
            run_id=control_run,
            sample_pct=50,
            path_exists=_path_exists,
        )
        is StageProfile.STANDARD
    )
    control_resolution = resolve_stage_profile_metadata(
        triage,
        chunk,
        "repo",
        run_id=control_run,
        sample_pct=50,
        path_exists=_path_exists,
    )
    assert control_resolution.stage_profile is StageProfile.STANDARD
    assert control_resolution.trivial_profile_eligible is True

    ineligible_resolution = resolve_stage_profile_metadata(
        triage,
        chunk,
        "repo",
        run_id=sampled_run,
        sample_pct=50,
        path_exists=lambda *_: False,
    )
    assert ineligible_resolution.stage_profile is StageProfile.STANDARD
    assert ineligible_resolution.trivial_profile_eligible is False


def test_synthesized_plan_is_deterministic_and_grounded():
    chunk = _chunk(files_expected=["src/app.py", "tests/test_app.py"])
    first = synthesize_trivial_plan(
        run_id="run-1",
        feature_description="enriched",
        chunk=chunk,
    )
    second = synthesize_trivial_plan(
        run_id="run-1",
        feature_description="enriched",
        chunk=chunk,
    )
    assert first == second
    assert first.handoff_from == "planner"
    assert first.handoff_to == "coder"
    assert first.files_to_create == []
    assert first.files_to_modify == chunk.files_expected
    assert first.files_to_read == chunk.files_expected
    assert first.steps == [
        "Implement chunk 1: Small edit",
        "Change the existing implementation.",
    ]


@pytest.mark.asyncio
async def test_feature_off_uses_standard_planner_path_and_null_ledger(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _touch_expected_files(tmp_repo, run_id)
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(chunked_orchestrator, "MERGED_PROFILE_SAMPLE_PCT", 0)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert any(call[0] == "planner" for call in calls)
    summary = _completion_summary(run_id)
    assert summary["key_decisions"] == ["Plan step", "Code step"]
    assert "plan_source_label" not in summary
    attempts = list_chunk_attempts(run_id, 1)
    assert attempts[0]["stage_profile"] is None
    assert attempts[0]["trivial_profile_eligible"] is None


@pytest.mark.asyncio
async def test_profiled_chunk_skips_only_planner_and_still_runs_reviewer(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _touch_expected_files(tmp_repo, run_id)
    calls: list[tuple] = []
    captured: dict[str, object] = {}
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(chunked_orchestrator, "MERGED_PROFILE_SAMPLE_PCT", 100)

    async def boom_planner(*_args, **_kwargs):
        raise AssertionError("profiled trivial chunk must not call planner")

    async def fake_coder(plan, run_id_arg, chunk_number=0, **_kwargs):
        captured["plan"] = plan
        calls.append(("coder", chunk_number))
        return make_coder_result(run_id_arg, chunk_number)

    monkeypatch.setattr(chunked_orchestrator, "run_planner", boom_planner)
    monkeypatch.setattr(chunked_orchestrator, "run_coder", fake_coder)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert not any(call[0] == "planner" for call in calls)
    assert [call[0] for call in calls if call[0] in {"coder", "dry_run", "patch", "test", "review"}] == [
        "coder",
        "dry_run",
        "patch",
        "test",
        "review",
    ]
    plan = captured["plan"]
    assert plan.goal == "Chunk 1"
    assert plan.files_to_create == []
    assert plan.files_to_modify == [
        "created_1.py",
        "modified_1.py",
        "deleted_1.py",
    ]
    summary = _completion_summary(run_id)
    assert summary["key_decisions"] == [
        "Implement chunk 1: Chunk 1",
        "Do chunk 1",
    ]
    assert (
        summary["plan_source_label"]
        == chunked_orchestrator.SYNTHESIZED_TRIVIAL_PLAN_LABEL
    )
    assert summary["plan_source"] == "synthesized_from_approved_triage"
    final_summary = _final_approval_summary(run_id)
    assert (
        f"Plan source: {chunked_orchestrator.SYNTHESIZED_TRIVIAL_PLAN_LABEL}"
        in final_summary
    )
    attempts = list_chunk_attempts(run_id, 1)
    assert attempts[0]["stage_profile"] == StageProfile.MERGED_PLAN_CODE.value
    assert attempts[0]["trivial_profile_eligible"] == 1
    stages = json.loads(attempts[0]["stage_outcomes_json"])
    assert stages[0]["stage"] == "plan"
    assert stages[0]["evidence"] == ["synthesized_from_triage"]


@pytest.mark.asyncio
async def test_eligible_unsampled_nonzero_profile_records_standard_control(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id = next(
        f"unsampled-{i}"
        for i in range(1000)
        if sampling_bucket(f"unsampled-{i}", 1) > 0
    )
    project = make_project(tmp_repo)
    tracked_runs.append(run_id)
    create_chunked_run(
        run_id,
        project["id"],
        "Execute chunks",
        make_triage(run_id, project["id"]),
    )
    approve_chunk_plan(run_id)
    _touch_expected_files(tmp_repo, run_id)
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    # Nonzero, but equal to the bucket, so bucket < pct is false: control cohort.
    monkeypatch.setattr(
        chunked_orchestrator,
        "MERGED_PROFILE_SAMPLE_PCT",
        sampling_bucket(run_id, 1),
    )

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert any(call[0] == "planner" for call in calls)
    summary = _completion_summary(run_id)
    assert "plan_source_label" not in summary
    assert (
        f"Plan source: {chunked_orchestrator.SYNTHESIZED_TRIVIAL_PLAN_LABEL}"
        not in _final_approval_summary(run_id)
    )
    attempts = list_chunk_attempts(run_id, 1)
    assert attempts[0]["stage_profile"] == StageProfile.STANDARD.value
    assert attempts[0]["trivial_profile_eligible"] == 1


@pytest.mark.asyncio
async def test_sampled_but_ineligible_new_file_chunk_uses_planner_and_standard_ledger(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(chunked_orchestrator, "MERGED_PROFILE_SAMPLE_PCT", 100)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    assert any(call[0] == "planner" for call in calls)
    attempts = list_chunk_attempts(run_id, 1)
    assert attempts[0]["stage_profile"] == StageProfile.STANDARD.value
    assert attempts[0]["trivial_profile_eligible"] == 0
    eligible_control = [
        attempt
        for attempt in attempts
        if attempt["entry_mode"] == chunk_driver.EntryMode.FRESH.value
        and attempt["trivial_profile_eligible"] == 1
        and attempt["stage_profile"] == StageProfile.STANDARD.value
    ]
    assert eligible_control == []


@pytest.mark.asyncio
async def test_stage_profile_does_not_change_scope_or_approval_state(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    _touch_expected_files(tmp_repo, run_id)
    before = _chunk_read_model(run_id)
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls)
    patch_success_pipeline(monkeypatch, run_id, calls)
    monkeypatch.setattr(chunked_orchestrator, "MERGED_PROFILE_SAMPLE_PCT", 100)

    result = await chunked_orchestrator.execute_approved_chunks(run_id)

    assert result["status"] == "awaiting_final_approval"
    after = _chunk_read_model(run_id)
    assert after == before
    assert after["files_expected"] == [
        "created_1.py",
        "modified_1.py",
        "deleted_1.py",
    ]


def test_trivial_profile_eligible_is_not_authority():
    runtime_sources = "\n".join(
        inspect.getsource(module)
        for module in (
            chunk_attempt_store,
            chunk_driver,
            chunked_orchestrator,
            database,
        )
    )
    forbidden_predicates = (
        "WHERE trivial_profile_eligible",
        "AND trivial_profile_eligible",
        "OR trivial_profile_eligible",
        "if trivial_profile_eligible",
        "elif trivial_profile_eligible",
        "while trivial_profile_eligible",
    )

    for predicate in forbidden_predicates:
        assert predicate not in runtime_sources


def test_chunk_attempt_stage_profile_values_and_legacy_null_safety(
    tmp_repo,
    tracked_runs,
):
    run_id = str(uuid.uuid4())
    tracked_runs.append(run_id)
    project_id = "project-ledger"

    merged = record_chunk_attempt(
        run_id=run_id,
        project_id=project_id,
        chunk_number=1,
        entry_mode=chunk_driver.EntryMode.FRESH.value,
        stage_outcomes=[],
        final_outcome_class="SUCCESS",
        final_status="completed",
        stage_profile=StageProfile.MERGED_PLAN_CODE.value,
        trivial_profile_eligible=True,
        head_sha="merged-head",
    )
    standard = record_chunk_attempt(
        run_id=run_id,
        project_id=project_id,
        chunk_number=2,
        entry_mode=chunk_driver.EntryMode.FRESH.value,
        stage_outcomes=[],
        final_outcome_class="SUCCESS",
        final_status="completed",
        stage_profile=StageProfile.STANDARD.value,
        trivial_profile_eligible=True,
        head_sha="standard-head",
    )
    legacy = record_chunk_attempt(
        run_id=run_id,
        project_id=project_id,
        chunk_number=3,
        entry_mode=chunk_driver.EntryMode.FRESH.value,
        stage_outcomes=[],
        final_outcome_class="SUCCESS",
        final_status="completed",
        head_sha="legacy-head",
    )

    assert merged["stage_profile"] == "merged_plan_code"
    assert merged["trivial_profile_eligible"] is True
    assert standard["stage_profile"] == "standard"
    assert standard["trivial_profile_eligible"] is True
    assert legacy["stage_profile"] is None
    assert legacy["trivial_profile_eligible"] is None
    attempts = list_chunk_attempts(run_id)
    assert [attempt["stage_profile"] for attempt in attempts] == [
        "merged_plan_code",
        "standard",
        None,
    ]
    assert [attempt["trivial_profile_eligible"] for attempt in attempts] == [
        1,
        1,
        None,
    ]
    assert get_latest_completed_attempt_head(run_id)["head_sha"] == "legacy-head"

    with engine.begin() as conn:
        database._ensure_chunk_attempts_shape(conn)
        database._ensure_chunk_attempts_shape(conn)
        columns = [
            row._mapping["name"]
            for row in conn.execute(text("PRAGMA table_info(chunk_attempts)")).fetchall()
        ]
    assert columns.count("stage_profile") == 1
    assert columns.count("trivial_profile_eligible") == 1


def test_chunk_attempt_shape_adds_trivial_profile_eligible_to_legacy_table():
    legacy_engine = create_engine("sqlite:///:memory:")
    with legacy_engine.begin() as conn:
        conn.execute(
            text("""
                CREATE TABLE chunk_attempts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    chunk_number INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    entry_mode TEXT NOT NULL,
                    stage_outcomes_json TEXT,
                    evidence_refs_json TEXT,
                    final_outcome_class TEXT,
                    final_status TEXT,
                    stage_profile TEXT,
                    head_sha TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(run_id, chunk_number, attempt_number)
                )
            """)
        )

        database._ensure_chunk_attempts_shape(conn)
        database._ensure_chunk_attempts_shape(conn)
        columns = [
            row._mapping["name"]
            for row in conn.execute(text("PRAGMA table_info(chunk_attempts)")).fetchall()
        ]

    assert columns.count("stage_profile") == 1
    assert columns.count("trivial_profile_eligible") == 1


@pytest.mark.asyncio
async def test_human_retry_records_null_stage_profile(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    failure_report_id = _seed_failed_chunk(run_id, 1)
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_hash",
        lambda repo: "retry-head",
    )

    result = await chunked_orchestrator.retry_failed_chunk(run_id, 1, failure_report_id)

    assert result["status"] == "awaiting_chunk_approval"
    attempts = list_chunk_attempts(run_id, 1)
    assert attempts[0]["entry_mode"] == chunk_driver.EntryMode.HUMAN_RETRY.value
    assert attempts[0]["stage_profile"] is None
    assert attempts[0]["trivial_profile_eligible"] is None


@pytest.mark.asyncio
async def test_steer_records_null_stage_profile(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, _project = create_run(tmp_repo, tracked_runs)
    failure_report_id = _seed_failed_chunk(
        run_id,
        1,
        failure_type=PatchFailureType.TEST_REGRESSION,
    )
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_hash",
        lambda repo: "steer-head",
    )

    result = await chunked_orchestrator.steer_chunk(
        run_id,
        1,
        failure_report_id,
        "Use the existing approved files only.",
    )

    assert result["status"] == "awaiting_chunk_approval"
    attempts = [
        attempt for attempt in list_chunk_attempts(run_id, 1)
        if attempt["entry_mode"] == chunk_driver.EntryMode.STEERED.value
    ]
    assert len(attempts) == 1
    assert attempts[0]["stage_profile"] is None
    assert attempts[0]["trivial_profile_eligible"] is None


@pytest.mark.asyncio
async def test_refinement_records_null_stage_profile(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    chunked_orchestrator.save_chunk_completion_summary(
        run_id,
        1,
        {
            "summary": "chunk committed",
            "chunk_description": "do chunk 1",
            "files_modified": ["modified_1.py"],
        },
    )
    chunked_orchestrator.update_chunk_status(run_id, 1, "completed")
    record_chunk_attempt(
        run_id=run_id,
        project_id=project["id"],
        chunk_number=1,
        entry_mode=chunk_driver.EntryMode.FRESH.value,
        final_outcome_class="SUCCESS",
        final_status="completed",
        head_sha="committed-head",
    )
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE pipeline_runs
                SET status = 'chunk_approved',
                    current_step = 'chunk_approved'
                WHERE id = :run_id
            """),
            {"run_id": run_id},
        )
    calls: list[tuple] = []
    patch_git_preflight(monkeypatch, calls, run_id=run_id)
    patch_retry_pipeline(monkeypatch, run_id, calls=calls)
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "get_current_hash",
        lambda repo: "refine-start-head",
    )
    monkeypatch.setattr(
        chunked_orchestrator.local_git,
        "show_commit",
        lambda sha, repo: "--- committed diff ---\n+old\n",
    )
    async def no_review(**_kwargs):
        return None

    monkeypatch.setattr(chunked_orchestrator, "run_chunk_review", no_review)

    result = await chunked_orchestrator.steer_chunk(
        run_id,
        1,
        None,
        "Refine the completed chunk within approved files.",
    )

    assert result["status"] == "awaiting_chunk_approval"
    attempts = [
        attempt for attempt in list_chunk_attempts(run_id, 1)
        if attempt["entry_mode"] == chunk_driver.EntryMode.STEERED.value
    ]
    assert len(attempts) == 1
    assert attempts[0]["stage_profile"] is None
    assert attempts[0]["trivial_profile_eligible"] is None


@pytest.mark.asyncio
async def test_resume_ignores_stage_profile_argument_and_records_null(
    monkeypatch, tmp_repo, tracked_runs,
):
    run_id, project = create_run(tmp_repo, tracked_runs)
    add_test_checkpoint(run_id, 1)
    plan = get_chunk_plan_status(run_id)
    chunk = plan.triage.chunks[0]
    chunk_status = plan.chunks[0]
    patch_resume_git(monkeypatch)

    result = await chunk_driver.drive_chunk(
        chunk_driver.EntryMode.RESUME,
        run_id=run_id,
        project_id=project["id"],
        chunk=chunk,
        target_repo_path=project["repo_path"],
        branch_name=f"pipewright/{run_id[:8]}",
        status_by_number={1: "pending"},
        chunk_status=chunk_status,
        stage_profile=StageProfile.MERGED_PLAN_CODE,
    )

    assert result.skipped is True
    attempts = list_chunk_attempts(run_id, 1)
    assert attempts[0]["entry_mode"] == chunk_driver.EntryMode.RESUME.value
    assert attempts[0]["stage_profile"] is None
    assert attempts[0]["trivial_profile_eligible"] is None
