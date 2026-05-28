"""
Tests for the strict scope-drift guard (scope_guard.py).

Pure unit tests. No DB, no FastAPI, no disk writes.
"""

import pytest

from backend.models.handoff import CoderHandoff, FileChange
from backend.pipeline.scope_guard import (
    ScopeDriftError,
    assert_files_in_scope,
)

pytestmark = pytest.mark.unit


def _coder(paths_and_actions: list[tuple[str, str]]) -> CoderHandoff:
    changes = [
        FileChange(
            path=path,
            action=action,
            content=None if action == "delete" else "print('x')\n",
            reason="test",
        )
        for path, action in paths_and_actions
    ]
    return CoderHandoff(
        run_id="run-1",
        feature_description="test",
        files_changed=changes,
        summary="test summary",
        suggested_memory_entries=[],
    )


def test_exact_match_in_scope_passes():
    coder = _coder([("README.md", "modify")])

    assert_files_in_scope(coder, ["README.md"]) is None


def test_path_outside_scope_raises():
    coder = _coder([("src/app.py", "modify")])

    with pytest.raises(ScopeDriftError) as error:
        assert_files_in_scope(coder, ["README.md"])

    message = str(error.value)
    assert "src/app.py" in message
    assert "README.md" in message


def test_message_names_offending_path_and_expected_scope():
    coder = _coder([
        ("backend/main.py", "modify"),
        ("docs/notes.md", "create"),
    ])

    with pytest.raises(ScopeDriftError) as error:
        assert_files_in_scope(coder, ["backend/main.py"])

    message = str(error.value)
    assert "docs/notes.md" in message
    assert "backend/main.py" in message


def test_backslash_paths_normalize_to_forward_slash():
    coder = _coder([("backend\\routes\\foo.py", "modify")])

    assert_files_in_scope(coder, ["backend/routes/foo.py"]) is None


def test_traversal_in_coder_path_is_rejected():
    coder = _coder([("../etc/passwd", "modify")])

    with pytest.raises(ScopeDriftError):
        assert_files_in_scope(coder, ["README.md"])


def test_absolute_coder_path_is_blocked_as_out_of_scope():
    coder = _coder([("/tmp/evil.py", "modify")])

    with pytest.raises(ScopeDriftError):
        assert_files_in_scope(coder, ["README.md"])


def test_empty_files_expected_is_blocked():
    coder = _coder([("README.md", "modify")])

    with pytest.raises(ScopeDriftError, match="empty files_expected"):
        assert_files_in_scope(coder, [])


def test_invalid_files_expected_entry_is_blocked():
    coder = _coder([("README.md", "modify")])

    with pytest.raises(ScopeDriftError, match="files_expected"):
        assert_files_in_scope(coder, [""])


def test_all_actions_must_stay_in_scope():
    coder = _coder([
        ("backend/main.py", "create"),
        ("backend/main.py", "modify"),
        ("backend/extra.py", "delete"),
    ])

    with pytest.raises(ScopeDriftError) as error:
        assert_files_in_scope(coder, ["backend/main.py"])

    assert "backend/extra.py" in str(error.value)


def test_multiple_expected_paths_all_allowed():
    coder = _coder([
        ("backend/main.py", "modify"),
        ("backend/utils.py", "create"),
    ])

    assert_files_in_scope(
        coder,
        ["backend/main.py", "backend/utils.py"],
    ) is None
