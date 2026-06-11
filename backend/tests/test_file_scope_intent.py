"""
test_file_scope_intent.py
Unit tests for deterministic user file-scope constraints + reconciliation
(PR #22A). No LLM, no filesystem walk. Reconciliation tests seed file_index
directly, mirroring test_plan_path_grounding.py.
"""

import uuid

import pytest
from sqlalchemy import text

from backend.db.database import engine
from backend.models.chunk import ChunkDefinition, TriageResult
from backend.pipeline.file_scope_intent import (
    SCOPE_NOTE_PREFIX,
    extract_user_file_constraints,
    reconcile_file_scope,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# extract_user_file_constraints
# --------------------------------------------------------------------------

@pytest.mark.parametrize("feature", [
    "create the calculator app with basic UI using only src/app.py and tests/test_app.py",
    "use only src/app.py and tests/test_app.py",
    "only use src/app.py and tests/test_app.py",
    "modify only src/app.py and tests/test_app.py",
    "only modify src/app.py and tests/test_app.py",
    "update only src/app.py and tests/test_app.py",
    "change only src/app.py and tests/test_app.py",
    "touch only src/app.py and tests/test_app.py",
])
def test_hard_allowlist_phrases(feature):
    c = extract_user_file_constraints(feature)
    assert c.hard_allowlist == ("src/app.py", "tests/test_app.py")
    assert c.preferred_files == ()
    assert c.forbidden_files == ()


@pytest.mark.parametrize("feature", [
    "add a feature but do not touch tests/test_app.py",
    "add a feature but don't touch tests/test_app.py",
    "add a feature but do not modify tests/test_app.py",
    "add a feature but don't modify tests/test_app.py",
    "add a feature without changing tests/test_app.py",
    "add a feature without modifying tests/test_app.py",
])
def test_forbidden_phrases(feature):
    c = extract_user_file_constraints(feature)
    assert c.forbidden_files == ("tests/test_app.py",)
    assert c.hard_allowlist == ()


@pytest.mark.parametrize("feature", [
    "build calculator similar to tests/test_app.py",
    "build calculator like tests/test_app.py",
    "build calculator look at tests/test_app.py",
    "build calculator see tests/test_app.py",
])
def test_reference_only_phrases(feature):
    c = extract_user_file_constraints(feature)
    assert c.reference_only_files == ("tests/test_app.py",)
    assert c.hard_allowlist == ()
    assert c.preferred_files == ()


def test_preferred_files_without_only():
    c = extract_user_file_constraints("update src/app.py and tests/test_app.py")
    assert c.preferred_files == ("src/app.py", "tests/test_app.py")
    assert c.hard_allowlist == ()


def test_forbidden_wins_over_allowlist_conflict():
    c = extract_user_file_constraints(
        "use only src/app.py but do not touch src/app.py"
    )
    assert c.forbidden_files == ("src/app.py",)
    assert "src/app.py" not in c.hard_allowlist


def test_non_path_tokens_are_not_constraints():
    c = extract_user_file_constraints("use only TCP/IP and and/or logic")
    assert c.is_empty


def test_no_file_mention_is_empty():
    assert extract_user_file_constraints("add a calculator feature").is_empty


# --------------------------------------------------------------------------
# E9: line/bullet-aware tokenization. Bulleted, numbered, and one-per-line
# lists after a cue must collect every file, exactly like the inline forms.
# --------------------------------------------------------------------------

def test_colon_inline_comma_list_collects_both_files():
    c = extract_user_file_constraints(
        "Only modify: src/app.py, tests/test_app.py"
    )
    assert c.hard_allowlist == ("src/app.py", "tests/test_app.py")
    assert c.uncertain_mentions == ()


def test_dash_bulleted_list_collects_both_files():
    c = extract_user_file_constraints(
        "Only modify:\n"
        "- backend/pipeline/tester.py\n"
        "- backend/tests/test_tester.py"
    )
    assert c.hard_allowlist == (
        "backend/pipeline/tester.py",
        "backend/tests/test_tester.py",
    )
    assert c.uncertain_mentions == ()


def test_star_bulleted_list_collects_both_files():
    c = extract_user_file_constraints(
        "Only modify:\n* src/app.py\n* tests/test_app.py"
    )
    assert c.hard_allowlist == ("src/app.py", "tests/test_app.py")


def test_numbered_list_collects_both_files():
    c = extract_user_file_constraints(
        "Only modify:\n1. src/app.py\n2. tests/test_app.py"
    )
    assert c.hard_allowlist == ("src/app.py", "tests/test_app.py")


def test_numbered_paren_list_collects_both_files():
    c = extract_user_file_constraints(
        "Only modify:\n1) src/app.py\n2) tests/test_app.py"
    )
    assert c.hard_allowlist == ("src/app.py", "tests/test_app.py")


def test_mixed_prose_and_bullets_collects_all_files():
    c = extract_user_file_constraints(
        "Only modify: src/app.py and\n- src/util.py\n- tests/test_app.py"
    )
    assert c.hard_allowlist == (
        "src/app.py", "src/util.py", "tests/test_app.py",
    )


def test_one_file_per_line_without_bullets_collects_both_files():
    c = extract_user_file_constraints(
        "Only modify:\nsrc/app.py\ntests/test_app.py"
    )
    assert c.hard_allowlist == ("src/app.py", "tests/test_app.py")


def test_bullets_without_cue_stay_uncertain():
    # No cue phrase: line-aware tokenization must not invent an allowlist.
    c = extract_user_file_constraints("- src/app.py\n- tests/test_app.py")
    assert c.hard_allowlist == ()
    assert c.preferred_files == ()
    assert c.uncertain_mentions == ("src/app.py", "tests/test_app.py")


def test_forbidden_bulleted_list_collects_forbidden_files():
    c = extract_user_file_constraints(
        "Do not touch:\n- tests/test_app.py\n- src/util.py"
    )
    assert c.forbidden_files == ("src/util.py", "tests/test_app.py")
    assert c.hard_allowlist == ()


def test_midline_hyphen_is_not_a_connector():
    # A hyphen inside a line still terminates the list; only a line-initial
    # bullet marker is list formatting.
    c = extract_user_file_constraints(
        "modify src/app.py - tests/test_app.py is unrelated"
    )
    assert c.preferred_files == ("src/app.py",)
    assert "tests/test_app.py" in c.uncertain_mentions


def test_version_number_at_line_start_is_not_a_bullet():
    # "3.11" is not a list marker: it must terminate collection rather than
    # be dropped (which would wrongly pull the next path into the allowlist).
    c = extract_user_file_constraints(
        "modify only src/app.py\n3.11 tests/test_app.py compatibility"
    )
    assert c.hard_allowlist == ("src/app.py",)
    assert "tests/test_app.py" in c.uncertain_mentions


# --------------------------------------------------------------------------
# reconcile_file_scope
# --------------------------------------------------------------------------

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
def calc_project():
    project_id = f"scope-{uuid.uuid4()}"
    _seed_index(project_id, [
        "src/app.py",
        "src/util.py",
        "tests/test_app.py",
    ])
    yield project_id
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM file_index WHERE project_id = :pid"),
            {"pid": project_id},
        )


def _single_chunk_triage(
    project_id: str,
    files_expected: list[str],
    *,
    description: str = "Implement the change.",
    rationale: str = "base rationale",
    feature: str = "do the work",
) -> TriageResult:
    return TriageResult(
        run_id="run-1",
        project_id=project_id,
        feature_description=feature,
        complexity="easy",
        total_chunks=1,
        reasoning="one chunk",
        chunks=[ChunkDefinition(
            chunk_number=1,
            title="Chunk",
            description=description,
            files_expected=files_expected,
            depends_on=[],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale=rationale,
        )],
    )


def test_repro_preserves_both_explicit_allowlist_files(calc_project):
    feature = (
        "create the calculator app with basic UI using only "
        "src/app.py and tests/test_app.py"
    )
    # Simulates the prior pipeline: pin kept only the first file.
    triage = _single_chunk_triage(calc_project, ["src/app.py"], feature=feature)
    constraints = extract_user_file_constraints(feature)

    result = reconcile_file_scope(calc_project, triage, constraints)

    assert result.chunks[0].files_expected == ["src/app.py", "tests/test_app.py"]


def test_hard_allowlist_caps_extra_planner_file(calc_project):
    feature = "update only src/app.py and src/util.py"
    triage = _single_chunk_triage(
        calc_project,
        ["src/app.py", "src/util.py", "src/secret_extra.py"],
        feature=feature,
    )
    constraints = extract_user_file_constraints(feature)

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    assert chunk.files_expected == ["src/app.py", "src/util.py"]
    assert chunk.requires_human_review is True
    assert chunk.risk_level == "high"
    assert SCOPE_NOTE_PREFIX in chunk.rationale
    assert "src/secret_extra.py" in chunk.rationale


def test_forbidden_file_removed_and_hardened(calc_project):
    feature = "add a calculator feature but do not touch tests/test_app.py"
    triage = _single_chunk_triage(
        calc_project,
        ["src/app.py", "tests/test_app.py"],
        feature=feature,
    )
    constraints = extract_user_file_constraints(feature)

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    assert chunk.files_expected == ["src/app.py"]
    assert "tests/test_app.py" not in chunk.files_expected
    assert chunk.requires_human_review is True
    # A forbidden-file removal is a real safety event: full hardening stays.
    assert chunk.risk_level == "high"
    assert SCOPE_NOTE_PREFIX in chunk.rationale


def test_reference_only_path_not_auto_added(calc_project):
    feature = "implement calculator similar to tests/test_app.py"
    triage = _single_chunk_triage(calc_project, ["src/app.py"], feature=feature)
    constraints = extract_user_file_constraints(feature)

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    assert chunk.files_expected == ["src/app.py"]
    assert "tests/test_app.py" not in chunk.files_expected


def test_planner_prose_mismatch_is_flagged_not_added(calc_project):
    # User named no files; planner prose references a real file omitted from scope.
    triage = _single_chunk_triage(
        calc_project,
        ["src/app.py"],
        description="Implement UI in src/app.py and add tests in tests/test_app.py.",
        feature="add a calculator feature",
    )
    constraints = extract_user_file_constraints("add a calculator feature")
    assert constraints.is_empty

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    # Not silently added.
    assert chunk.files_expected == ["src/app.py"]
    # Surfaced for a human — but a note-only mismatch never inflates
    # risk_level (E9); scope was not changed.
    assert chunk.requires_human_review is True
    assert chunk.risk_level == "low"
    assert SCOPE_NOTE_PREFIX in chunk.rationale
    assert "tests/test_app.py" in chunk.rationale


def test_no_file_mention_leaves_plan_unchanged(calc_project):
    triage = _single_chunk_triage(
        calc_project,
        ["src/app.py"],
        description="Add the calculator feature.",
        feature="add a calculator feature",
    )
    constraints = extract_user_file_constraints("add a calculator feature")

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    assert chunk.files_expected == ["src/app.py"]
    assert chunk.requires_human_review is False
    assert chunk.risk_level == "low"
    assert chunk.rationale == "base rationale"


def test_single_explicit_target_without_constraint_phrase_unchanged(calc_project):
    # "add hello in src/app.py" — a single explicit target, no only/forbid/etc.
    triage = _single_chunk_triage(
        calc_project,
        ["src/app.py"],
        description="Append content.",
        feature="add hello in src/app.py",
    )
    constraints = extract_user_file_constraints("add hello in src/app.py")

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    assert chunk.files_expected == ["src/app.py"]
    assert chunk.requires_human_review is False


def test_ungrounded_allowlist_file_warns_not_silently_dropped(calc_project):
    # configs/ is not an indexed directory, so configs/missing.py is ungroundable.
    feature = "use only src/app.py and configs/missing.py"
    triage = _single_chunk_triage(calc_project, ["src/app.py"], feature=feature)
    constraints = extract_user_file_constraints(feature)

    chunk = reconcile_file_scope(calc_project, triage, constraints).chunks[0]

    # Ungrounded file is not forced into enforced scope, but it is surfaced.
    assert chunk.files_expected == ["src/app.py"]
    assert chunk.requires_human_review is True
    assert "configs/missing.py" in chunk.rationale


# --------------------------------------------------------------------------
# E9: run-aware plan-consistency check
# --------------------------------------------------------------------------

def _two_chunk_triage(
    project_id: str,
    *,
    chunk1_files: list[str],
    chunk2_files: list[str],
    chunk1_description: str = "Implement the change.",
) -> TriageResult:
    def _chunk(number: int, description: str, files: list[str]) -> ChunkDefinition:
        return ChunkDefinition(
            chunk_number=number,
            title=f"Chunk {number}",
            description=description,
            files_expected=files,
            depends_on=[] if number == 1 else [number - 1],
            risk_level="low",
            token_estimate=100,
            requires_human_review=False,
            rationale="base rationale",
        )

    return TriageResult(
        run_id="run-1",
        project_id=project_id,
        feature_description="do the work",
        complexity="medium",
        total_chunks=2,
        reasoning="two chunks",
        chunks=[
            _chunk(1, chunk1_description, chunk1_files),
            _chunk(2, "Finish the change.", chunk2_files),
        ],
    )


def test_prose_path_scoped_in_another_chunk_produces_no_note(calc_project):
    # Chunk 1 prose mentions tests/test_app.py, which is planned work in
    # chunk 2's files_expected. That is not a mismatch: no note, no review,
    # no risk escalation.
    triage = _two_chunk_triage(
        calc_project,
        chunk1_files=["src/app.py"],
        chunk2_files=["tests/test_app.py"],
        chunk1_description="Implement UI in src/app.py; tests/test_app.py covers it later.",
    )
    constraints = extract_user_file_constraints("add a calculator feature")
    assert constraints.is_empty

    result = reconcile_file_scope(calc_project, triage, constraints)
    chunk1 = result.chunks[0]

    assert chunk1.files_expected == ["src/app.py"]
    assert chunk1.requires_human_review is False
    assert chunk1.risk_level == "low"
    assert SCOPE_NOTE_PREFIX not in (chunk1.rationale or "")


def test_genuinely_unscoped_prose_path_notes_review_without_high_risk(calc_project):
    # src/util.py is indexed and mentioned in prose but in no chunk's scope:
    # keep the [SCOPE] note, require human review, never force risk to high.
    triage = _two_chunk_triage(
        calc_project,
        chunk1_files=["src/app.py"],
        chunk2_files=["tests/test_app.py"],
        chunk1_description="Implement UI in src/app.py and tweak src/util.py.",
    )
    constraints = extract_user_file_constraints("add a calculator feature")

    result = reconcile_file_scope(calc_project, triage, constraints)
    chunk1 = result.chunks[0]

    assert chunk1.files_expected == ["src/app.py"]
    assert chunk1.requires_human_review is True
    assert chunk1.risk_level == "low"
    assert SCOPE_NOTE_PREFIX in chunk1.rationale
    assert "src/util.py" in chunk1.rationale
