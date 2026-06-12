"""
test_patch_dry_run.py
Tests for the shared dry-run/apply evaluator (#26B).

These exercise the pure, read-only evaluator in patch_dry_run.py and prove that:
  - find_unique_match has exact-match semantics,
  - evaluate_file_change preserves the existing RuntimeError message strings,
  - dry_run_changes performs ZERO disk mutation, and
  - the dry-run verdict matches the real apply_patch outcome category
    (deterministic parity, no random/flaky cases).

No API calls. Uses a local .pytest_tmp folder (Windows system temp denies access).
"""

import shutil
import uuid
from pathlib import Path

import pytest

from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline.patch_dry_run import (
    DryRunResult,
    EvaluatedChange,
    LineEndingStyle,
    MatchStatus,
    detect_line_ending_style,
    dry_run_changes,
    evaluate_file_change,
    find_unique_match,
)
from backend.pipeline.patch_applier import (
    BACKUP_DIR,
    apply_patch,
    classify_patch_failure,
)
from backend.pipeline.patch_failures import PatchFailureType

pytestmark = pytest.mark.unit

LOCAL_TMP = Path(__file__).parent.parent / ".pytest_tmp"


@pytest.fixture()
def tmp_repo():
    folder = LOCAL_TMP / str(uuid.uuid4())
    folder.mkdir(parents=True, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def _output(run_id: str, *changes: FileChange) -> CoderHandoff:
    return CoderHandoff(
        run_id=run_id,
        feature_description="dry-run test",
        files_changed=list(changes),
        summary="dry-run test",
    )


def _set_target_repo(monkeypatch, tmp_repo):
    from backend.config import keys
    import backend.pipeline.patch_applier as patch_applier

    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))
    monkeypatch.setattr(patch_applier, "save_checkpoint", lambda **kwargs: None)


# --------------------------------------------------------------------------- #
# find_unique_match
# --------------------------------------------------------------------------- #


def test_find_unique_match_absent():
    result = find_unique_match("a = 1\nb = 2\n", "zzz")
    assert result.status == MatchStatus.ABSENT
    assert result.count == 0
    assert result.ok is False


def test_find_unique_match_unique():
    result = find_unique_match("a = 1\nb = 2\n", "a = 1")
    assert result.status == MatchStatus.OK
    assert result.count == 1
    assert result.ok is True


def test_find_unique_match_non_unique():
    result = find_unique_match("dup = 1\ndup = 1\n", "dup = 1")
    assert result.status == MatchStatus.NON_UNIQUE
    assert result.count == 2
    assert result.ok is False


def test_detect_line_ending_style_classifies_mixed_eol():
    assert detect_line_ending_style("a\r\nb\n") == LineEndingStyle.MIXED


# --------------------------------------------------------------------------- #
# evaluate_file_change — message-string contract preserved
# --------------------------------------------------------------------------- #


def test_evaluate_valid_edit_returns_new_content(tmp_repo):
    (tmp_repo / "f.py").write_bytes(b"value = 1\n")
    change = FileChange(
        path="f.py", action="edit",
        old_string="value = 1", new_string="value = 2", reason="r",
    )
    result = evaluate_file_change(change, str(tmp_repo))
    assert isinstance(result, EvaluatedChange)
    assert result.new_content == "value = 2\n"
    assert result.action == "edit"
    assert result.path == "f.py"


def test_evaluate_edit_old_string_absent_message(tmp_repo):
    (tmp_repo / "f.py").write_text("a = 1\n", encoding="utf-8")
    change = FileChange(
        path="f.py", action="edit",
        old_string="missing = 0", new_string="x = 0", reason="r",
    )
    with pytest.raises(RuntimeError, match="old_string not found"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_edit_old_string_non_unique_message(tmp_repo):
    (tmp_repo / "f.py").write_text("dup = 1\ndup = 1\n", encoding="utf-8")
    change = FileChange(
        path="f.py", action="edit",
        old_string="dup = 1", new_string="dup = 2", reason="r",
    )
    with pytest.raises(RuntimeError, match="not unique"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_create_target_exists_message(tmp_repo):
    (tmp_repo / "exists.py").write_text("x = 1\n", encoding="utf-8")
    change = FileChange(
        path="exists.py", action="create", content="y = 2\n", reason="r"
    )
    with pytest.raises(RuntimeError, match="create target already exists"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_edit_target_missing_message(tmp_repo):
    change = FileChange(
        path="missing.py", action="edit",
        old_string="a", new_string="b", reason="r",
    )
    with pytest.raises(RuntimeError, match="edit target missing"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_modify_target_missing_message(tmp_repo):
    change = FileChange(
        path="missing.py", action="modify", content="x\n", reason="r"
    )
    with pytest.raises(RuntimeError, match="modify target missing"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_large_file_wholesale_modify_blocked(tmp_repo):
    big = tmp_repo / "BIG.md"
    big.write_text(
        "\n".join(f"Line {i}" for i in range(238)) + "\n", encoding="utf-8"
    )
    change = FileChange(
        path="BIG.md", action="modify", content="tiny\n", reason="r"
    )
    with pytest.raises(RuntimeError, match="cannot be replaced wholesale"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_forbidden_path_message(tmp_repo):
    change = FileChange(
        path=".env", action="edit",
        old_string="A", new_string="B", reason="r",
    )
    with pytest.raises(RuntimeError, match="forbidden path"):
        evaluate_file_change(change, str(tmp_repo))


def test_evaluate_invalid_action_message(tmp_repo):
    change = FileChange(path="x.py", action="frobnicate", reason="r")
    with pytest.raises(RuntimeError, match="invalid action"):
        evaluate_file_change(change, str(tmp_repo))


# --------------------------------------------------------------------------- #
# no-op edit is appliable; large-file targeted edit allowed
# --------------------------------------------------------------------------- #


def test_evaluate_noop_edit_is_appliable(tmp_repo):
    # new_string identical to old_string: appliable at the evaluator layer.
    # NO_CHANGES is decided later, outside the evaluator.
    (tmp_repo / "f.py").write_bytes(b"value = 1\n")
    change = FileChange(
        path="f.py", action="edit",
        old_string="value = 1", new_string="value = 1", reason="r",
    )
    result = evaluate_file_change(change, str(tmp_repo))
    assert result.new_content == "value = 1\n"


def test_evaluate_large_file_targeted_edit_allowed(tmp_repo):
    # The large-file guard only blocks wholesale `modify`; a targeted `edit`
    # on a large file remains allowed.
    lines = [f"row_{i} = {i}" for i in range(1000)]
    (tmp_repo / "big.py").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    change = FileChange(
        path="big.py", action="edit",
        old_string="row_500 = 500", new_string="row_500 = 5000", reason="r",
    )
    result = evaluate_file_change(change, str(tmp_repo))
    assert "row_500 = 5000" in result.new_content


# --------------------------------------------------------------------------- #
# dry_run_changes — zero mutation, all-or-nothing, pre-write only
# --------------------------------------------------------------------------- #


def test_dry_run_does_not_mutate_disk(tmp_repo):
    existing = tmp_repo / "existing.py"
    existing.write_text("value = 1\n", encoding="utf-8")
    before = existing.read_text(encoding="utf-8")

    run_id = str(uuid.uuid4())
    output = _output(
        run_id,
        FileChange(path="new.py", action="create", content="x = 1\n", reason="r"),
        FileChange(
            path="existing.py", action="edit",
            old_string="value = 1", new_string="value = 2", reason="r",
        ),
    )

    result = dry_run_changes(output, str(tmp_repo))

    assert isinstance(result, DryRunResult)
    assert result.ok is True
    # The would-be create did not happen and the existing file is untouched.
    assert not (tmp_repo / "new.py").exists()
    assert existing.read_text(encoding="utf-8") == before


def test_dry_run_does_not_create_backups_or_manifests(tmp_repo):
    (tmp_repo / "existing.py").write_text("value = 1\n", encoding="utf-8")
    run_id = str(uuid.uuid4())
    output = _output(
        run_id,
        FileChange(
            path="existing.py", action="edit",
            old_string="value = 1", new_string="value = 2", reason="r",
        ),
    )

    dry_run_changes(output, str(tmp_repo))

    assert not (BACKUP_DIR / run_id).exists()


def test_dry_run_multi_file_failure_writes_nothing(tmp_repo):
    # First change is a valid create; second targets a missing file. Dry-run is
    # all-or-nothing and writes nothing, so the valid file must never appear.
    run_id = str(uuid.uuid4())
    output = _output(
        run_id,
        FileChange(path="a.py", action="create", content="a = 1\n", reason="ok"),
        FileChange(
            path="missing.py", action="edit",
            old_string="x", new_string="y", reason="bad",
        ),
    )

    result = dry_run_changes(output, str(tmp_repo))

    assert result.ok is False
    assert result.failed_path == "missing.py"
    assert result.failed_action == "edit"
    assert "edit target missing" in result.error_message
    assert not (tmp_repo / "a.py").exists()
    assert not (BACKUP_DIR / run_id).exists()


def test_dry_run_ok_for_valid_prewrite_changes_only(tmp_repo):
    # A no-op edit is a valid PRE-WRITE change; the dry-run must report ok and
    # must NOT over-classify it as NO_CHANGES (decided later, elsewhere).
    (tmp_repo / "f.py").write_text("value = 1\n", encoding="utf-8")
    run_id = str(uuid.uuid4())
    output = _output(
        run_id,
        FileChange(
            path="f.py", action="edit",
            old_string="value = 1", new_string="value = 1", reason="r",
        ),
    )

    result = dry_run_changes(output, str(tmp_repo))

    assert result.ok is True
    assert result.error_message is None
    assert len(result.evaluated) == 1


def test_dry_run_predicted_eol_bytes_match_real_apply(tmp_repo, monkeypatch):
    _set_target_repo(monkeypatch, tmp_repo)
    target = tmp_repo / "crlf.py"
    target.write_bytes(b"alpha = 1\r\nbeta = 2\r\n")
    run_id = str(uuid.uuid4())
    output = _output(
        run_id,
        FileChange(
            path="crlf.py",
            action="edit",
            old_string="alpha = 1\nbeta = 2",
            new_string="alpha = 1\nbeta = 3",
            reason="r",
        ),
    )

    dry = dry_run_changes(output, str(tmp_repo))

    assert dry.ok is True
    predicted = dry.evaluated[0].new_content.encode("utf-8")
    assert predicted == b"alpha = 1\r\nbeta = 3\r\n"

    apply_patch(output, run_id)

    assert target.read_bytes() == predicted


# --------------------------------------------------------------------------- #
# Deterministic parity: dry-run verdict == real apply_patch outcome category
# --------------------------------------------------------------------------- #


def test_dry_run_parity_with_real_apply(tmp_repo, monkeypatch):
    """
    For a fixed set of cases, the dry-run ok/failure (and failure category, via
    classify_patch_failure) must match the real apply_patch outcome. This is the
    structural guarantee that the two cannot drift. Deterministic — no randomness.
    """
    from backend.config import keys
    import backend.pipeline.patch_applier as patch_applier

    monkeypatch.setattr(keys.settings, "target_repo_path", str(tmp_repo))
    # Isolate from the checkpoint store on the apply success path.
    monkeypatch.setattr(patch_applier, "save_checkpoint", lambda **kwargs: None)

    def seed_create_exists():
        (tmp_repo / "p_create_exists.py").write_text("x = 1\n", encoding="utf-8")

    def seed_edit_ok():
        (tmp_repo / "p_edit_ok.py").write_text("value = 1\n", encoding="utf-8")

    def seed_edit_absent():
        (tmp_repo / "p_edit_absent.py").write_text("a = 1\n", encoding="utf-8")

    def seed_edit_dup():
        (tmp_repo / "p_edit_dup.py").write_text("dup\ndup\n", encoding="utf-8")

    def seed_big_modify():
        (tmp_repo / "p_big.md").write_text(
            "\n".join(f"Line {i}" for i in range(238)) + "\n", encoding="utf-8"
        )

    cases = [
        # name, seed_fn, change, expected_ok, expected_type
        (
            "create_new",
            lambda: None,
            FileChange(path="p_create_new.py", action="create", content="x\n", reason="r"),
            True, None,
        ),
        (
            "create_exists",
            seed_create_exists,
            FileChange(path="p_create_exists.py", action="create", content="y\n", reason="r"),
            False, PatchFailureType.PATCH_DOES_NOT_APPLY,
        ),
        (
            "edit_ok",
            seed_edit_ok,
            FileChange(path="p_edit_ok.py", action="edit", old_string="value = 1", new_string="value = 2", reason="r"),
            True, None,
        ),
        (
            "edit_absent",
            seed_edit_absent,
            FileChange(path="p_edit_absent.py", action="edit", old_string="missing", new_string="x", reason="r"),
            False, PatchFailureType.PATCH_DOES_NOT_APPLY,
        ),
        (
            "edit_dup",
            seed_edit_dup,
            FileChange(path="p_edit_dup.py", action="edit", old_string="dup", new_string="x", reason="r"),
            False, PatchFailureType.PATCH_DOES_NOT_APPLY,
        ),
        (
            "edit_missing",
            lambda: None,
            FileChange(path="p_edit_missing.py", action="edit", old_string="a", new_string="b", reason="r"),
            False, PatchFailureType.TARGET_MISSING,
        ),
        (
            "modify_large",
            seed_big_modify,
            FileChange(path="p_big.md", action="modify", content="tiny\n", reason="r"),
            False, PatchFailureType.PATCH_MALFORMED,
        ),
        (
            "forbidden",
            lambda: None,
            FileChange(path=".env", action="edit", old_string="A", new_string="B", reason="r"),
            False, PatchFailureType.FORBIDDEN_FILE,
        ),
    ]

    for name, seed_fn, change, expected_ok, expected_type in cases:
        seed_fn()
        run_id = str(uuid.uuid4())
        output = _output(run_id, change)

        # Dry-run verdict (read-only, before any apply).
        dry = dry_run_changes(output, str(tmp_repo))
        assert dry.ok == expected_ok, f"{name}: dry-run ok mismatch"
        if not expected_ok:
            dry_type = classify_patch_failure(
                RuntimeError(dry.error_message), phase="apply"
            )
            assert dry_type == expected_type, f"{name}: dry-run category mismatch"

        # Real apply outcome category.
        try:
            apply_patch(output, run_id)
            real_ok, real_type = True, None
        except RuntimeError as error:
            real_ok = False
            real_type = classify_patch_failure(error, phase="apply")

        assert real_ok == expected_ok, f"{name}: real apply ok mismatch"
        if not expected_ok:
            assert real_type == expected_type, f"{name}: real apply category mismatch"
