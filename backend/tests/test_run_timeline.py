import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db.database import engine
from backend.main import app
from backend.pipeline.run_timeline import build_run_timeline

pytestmark = pytest.mark.unit


client = TestClient(app)

TIMELINE_TABLES = (
    "memory_injection_events",
    "chunk_reviews",
    "review_finding_acknowledgements",
    "test_validation_acknowledgements",
    "scope_expansion_requests",
    "chunk_attempts",
    "chunks",
    "approval_gates",
    "plan_versions",
    "pipeline_runs",
)


@pytest.fixture()
def tracked_runs():
    run_ids: list[str] = []
    yield run_ids
    with engine.begin() as conn:
        for run_id in run_ids:
            for table in TIMELINE_TABLES[:-1]:
                conn.execute(
                    text(f"DELETE FROM {table} WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
            conn.execute(
                text("DELETE FROM pipeline_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _fixture_ids(run_id: str) -> dict[str, str]:
    suffix = run_id.rsplit("-", maxsplit=1)[-1][:12]
    return {
        "plan": f"plan-version-{suffix}",
        "chunk": f"chunk-{suffix}",
        "attempt": f"attempt-{suffix}",
        "review": f"review-{suffix}",
        "test_ack": f"test-ack-{suffix}",
        "review_ack": f"review-ack-{suffix}",
        "gate": f"gate-{suffix}",
        "scope": f"scope-{suffix}",
        "memory": f"memory-{suffix}",
    }


def _insert_minimal_run(
    tracked_runs: list[str],
    *,
    run_id: str | None = None,
    project_id: str = "timeline-project",
    created_at: str = "2026-06-16T10:00:00+00:00",
    status: str = "running",
    pushed_at: str | None = None,
    pr_created_at: str | None = None,
    branch_name: str | None = None,
) -> str:
    run_id = run_id or _new_id("timeline-run")
    tracked_runs.append(run_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO pipeline_runs (
                    id, project_id, feature_description, status, current_step,
                    intent, chunk_plan_status, total_chunks,
                    current_chunk_number, branch_name, pushed_at,
                    pr_created_at, created_at
                )
                VALUES (
                    :id, :project_id, 'Build a timeline read model', :status,
                    'planning', 'implementation', 'none', 0, 0, :branch_name,
                    :pushed_at, :pr_created_at, :created_at
                )
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "status": status,
                "branch_name": branch_name,
                "pushed_at": pushed_at,
                "pr_created_at": pr_created_at,
                "created_at": created_at,
            },
        )
    return run_id


def _insert_full_timeline_fixture(
    tracked_runs: list[str],
    *,
    same_ts: str = "2026-06-16T12:00:00+00:00",
) -> str:
    run_id = _insert_minimal_run(
        tracked_runs,
        project_id="timeline-project-a",
        created_at=same_ts,
        pushed_at="2026-06-16T12:10:00+00:00",
        pr_created_at="2026-06-16T12:11:00+00:00",
        branch_name="feature/timeline",
    )
    ids = _fixture_ids(run_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO plan_versions (
                    id, run_id, version, triage_json, source, created_at
                )
                VALUES (
                    :id, :run_id, 1, :triage_json, 'initial', :created_at
                )
                """
            ),
            {
                "id": ids["plan"],
                "run_id": run_id,
                "triage_json": json.dumps({"chunks": []}),
                "created_at": same_ts,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO chunks (
                    id, run_id, project_id, chunk_number, title, description,
                    files_expected, risk_level, requires_human_review, status,
                    test_run_verdict, created_at
                )
                VALUES (
                    :id, :run_id, 'timeline-project-a', 1, 'First chunk',
                    'Implement the first chunk.', '["backend/pipeline/x.py"]',
                    'medium', 1, 'awaiting_approval', 'strong', :created_at
                )
                """
            ),
            {"id": ids["chunk"], "run_id": run_id, "created_at": same_ts},
        )
        conn.execute(
            text(
                """
                INSERT INTO chunk_attempts (
                    id, run_id, project_id, chunk_number, attempt_number,
                    entry_mode, final_outcome_class, final_status,
                    stage_profile, trivial_profile_eligible, created_at
                )
                VALUES (
                    :id, :run_id, 'timeline-project-a', 1, 1, 'normal',
                    'success', 'completed', 'standard', 0, :created_at
                )
                """
            ),
            {
                "id": ids["attempt"],
                "run_id": run_id,
                "created_at": "2026-06-16T12:01:00+00:00",
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO chunk_reviews (
                    id, run_id, chunk_number, review_status, verdict,
                    findings_json, created_at
                )
                VALUES (
                    :id, :run_id, 1, 'completed', 'pass', '[]', :created_at
                )
                """
            ),
            {"id": ids["review"], "run_id": run_id, "created_at": same_ts},
        )
        conn.execute(
            text(
                """
                INSERT INTO test_validation_acknowledgements (
                    id, run_id, chunk_number, verdict, acknowledged_diff_hash,
                    status, created_at
                )
                VALUES (
                    :id, :run_id, 1, 'weak', 'hash-test', 'active', :created_at
                )
                """
            ),
            {"id": ids["test_ack"], "run_id": run_id, "created_at": same_ts},
        )
        conn.execute(
            text(
                """
                INSERT INTO review_finding_acknowledgements (
                    id, run_id, chunk_number, acknowledged_diff_hash,
                    status, created_at
                )
                VALUES (
                    :id, :run_id, 1, 'hash-review', 'active', :created_at
                )
                """
            ),
            {"id": ids["review_ack"], "run_id": run_id, "created_at": same_ts},
        )
        conn.execute(
            text(
                """
                INSERT INTO approval_gates (
                    id, run_id, step, status, risk_level, chunk_number,
                    approval_type, decided_at, created_at
                )
                VALUES (
                    :id, :run_id, 'chunk_approval', 'approved', 'medium', 1,
                    'chunk', '2026-06-16T12:02:00+00:00', :created_at
                )
                """
            ),
            {"id": ids["gate"], "run_id": run_id, "created_at": same_ts},
        )
        conn.execute(
            text(
                """
                INSERT INTO scope_expansion_requests (
                    id, run_id, project_id, chunk_number, failure_report_id,
                    requested_files, approved_files, status, decided_at,
                    created_at
                )
                VALUES (
                    :id, :run_id, 'timeline-project-a', 1, 'failure-1',
                    '["backend/a.py", "backend/b.py"]', '["backend/a.py"]',
                    'approved', '2026-06-16T12:03:00+00:00', :created_at
                )
                """
            ),
            {"id": ids["scope"], "run_id": run_id, "created_at": same_ts},
        )
        conn.execute(
            text(
                """
                INSERT INTO memory_injection_events (
                    id, run_id, project_id, chunk_number, role,
                    attempt_number, token_budget, category_policy,
                    entries_json, included_count, excluded_count,
                    entries_hash, created_at
                )
                VALUES (
                    :id, :run_id, 'timeline-project-a', 1, 'coder', 1, 2048,
                    '["architecture"]', '{"included":[],"excluded":[]}',
                    0, 0, 'hash-memory', :created_at
                )
                """
            ),
            {"id": ids["memory"], "run_id": run_id, "created_at": same_ts},
        )
    return run_id


def _entry_by_id(entries: list[dict], entry_id: str) -> dict:
    return next(entry for entry in entries if entry["id"] == entry_id)


def test_unknown_run_returns_404():
    response = client.get(f"/runs/{_new_id('missing')}/timeline")

    assert response.status_code == 404


def test_empty_minimal_run_returns_valid_small_timeline(tracked_runs):
    run_id = _insert_minimal_run(tracked_runs)

    response = client.get(f"/runs/{run_id}/timeline")

    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 1
    assert entries[0] == {
        "id": "run:created",
        "ts": "2026-06-16T10:00:00+00:00",
        "kind": "run_created",
        "stage": "run",
        "chunk_number": None,
        "level": "info",
        "category": "lifecycle",
        "title": "Run created",
        "detail": "Pipeline run persisted.",
        "source": "persisted",
        "data": {
            "run_id": run_id,
            "project_id": "timeline-project",
            "status": "running",
            "current_step": "planning",
            "intent": "implementation",
            "chunk_plan_status": "none",
            "total_chunks": 0,
            "current_chunk_number": 0,
        },
    }


def test_timeline_ordering_uses_timestamp_then_stable_category_kind_tiebreak(
    tracked_runs,
):
    run_id = _insert_full_timeline_fixture(tracked_runs)
    ids = _fixture_ids(run_id)

    entries = build_run_timeline(run_id)

    assert entries is not None
    assert [entry["ts"] for entry in entries] == sorted(
        entry["ts"] for entry in entries
    )
    same_tick = [
        (entry["category"], entry["kind"], entry["id"])
        for entry in entries
        if entry["ts"] == "2026-06-16T12:00:00+00:00"
    ]
    assert same_tick == [
        ("lifecycle", "run_created", "run:created"),
        ("plan", "plan_created", f"plan_version:1:{ids['plan']}"),
        ("execution", "chunk_created", f"chunk:1:{ids['chunk']}:created"),
        ("execution", "scope_expansion_requested", f"scope_expansion:{ids['scope']}:created"),
        ("review", "review_recorded", f"chunk_review:1:{ids['review']}"),
        ("review", "acknowledgement_recorded", f"review_finding_ack:{ids['review_ack']}"),
        ("review", "acknowledgement_recorded", f"test_validation_ack:{ids['test_ack']}"),
        ("approval", "approval_required", f"approval_gate:{ids['gate']}:created"),
        ("memory", "memory_injected", f"memory_injection:{ids['memory']}"),
    ]


def test_timeline_stable_ids_and_idempotent_rederivation(tracked_runs):
    run_id = _insert_full_timeline_fixture(tracked_runs)

    first = build_run_timeline(run_id)
    second = build_run_timeline(run_id)
    response = client.get(f"/runs/{run_id}/timeline")

    assert first == second
    assert response.status_code == 200
    assert response.json() == first
    assert len({entry["id"] for entry in first}) == len(first)


def test_timeline_category_mapping_covers_persisted_sources(tracked_runs):
    run_id = _insert_full_timeline_fixture(tracked_runs)
    ids = _fixture_ids(run_id)

    entries = build_run_timeline(run_id)

    assert entries is not None
    assert _entry_by_id(entries, "run:created")["category"] == "lifecycle"
    assert _entry_by_id(entries, f"plan_version:1:{ids['plan']}")["category"] == "plan"
    assert _entry_by_id(entries, f"chunk:1:{ids['chunk']}:created")["category"] == "execution"
    assert _entry_by_id(entries, f"chunk_attempt:1:1:{ids['attempt']}")["category"] == "execution"
    assert _entry_by_id(entries, f"chunk_review:1:{ids['review']}")["category"] == "review"
    assert _entry_by_id(entries, f"test_validation_ack:{ids['test_ack']}")["category"] == "review"
    assert _entry_by_id(entries, f"review_finding_ack:{ids['review_ack']}")["category"] == "review"
    assert _entry_by_id(entries, f"approval_gate:{ids['gate']}:created")["category"] == "approval"
    assert _entry_by_id(entries, f"approval_gate:{ids['gate']}:decided")["category"] == "approval"
    assert _entry_by_id(entries, f"scope_expansion:{ids['scope']}:created")["category"] == "execution"
    assert _entry_by_id(entries, f"scope_expansion:{ids['scope']}:decided")["category"] == "execution"
    assert _entry_by_id(entries, f"memory_injection:{ids['memory']}")["category"] == "memory"
    assert _entry_by_id(entries, "run:git:pushed")["category"] == "ship"
    assert _entry_by_id(entries, "run:pr:created")["category"] == "ship"
    assert {entry["source"] for entry in entries} == {"persisted"}


def test_timeline_data_redacts_or_omits_sensitive_payloads(tracked_runs):
    run_id = _insert_minimal_run(
        tracked_runs,
        created_at="2026-06-16T13:00:00+00:00",
        pushed_at="2026-06-16T13:05:00+00:00",
        branch_name="feature/ghp_SECRET1234567890",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE pipeline_runs
                SET push_error = :push_error
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "push_error": "fatal: could not read token sk-SECRET1234567890",
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO approval_gates (
                    id, run_id, step, status, diff, test_results, ai_summary,
                    rejection_reason, created_at
                )
                VALUES (
                    'gate-redaction', :run_id, 'chunk_approval',
                    'pending token=SECRET1234567890', :diff_payload,
                    :stack_payload, :secret_payload, :git_payload,
                    '2026-06-16T13:01:00+00:00'
                )
                """
            ),
            {
                "run_id": run_id,
                "diff_payload": "diff --git a/app.py b/app.py\n@@ secret",
                "stack_payload": "Traceback (most recent call last):\nboom",
                "secret_payload": "provider returned sk-SECRET1234567890",
                "git_payload": "fatal: authentication failed for ghp_SECRET1234567890",
            },
        )

    entries = build_run_timeline(run_id)

    assert entries is not None
    serialized = json.dumps(entries, sort_keys=True)
    assert "ghp_SECRET1234567890" not in serialized
    assert "sk-SECRET1234567890" not in serialized
    assert "token=SECRET1234567890" not in serialized
    assert "diff --git" not in serialized
    assert "Traceback" not in serialized
    assert "fatal:" not in serialized
    push_entry = _entry_by_id(entries, "run:git:pushed")
    assert push_entry["data"]["branch_name"] == "feature/[redacted]"
