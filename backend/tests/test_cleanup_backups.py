"""
test_cleanup_backups.py
Unit tests for scripts/cleanup_backups.py

All tests use tmp_path and never touch the real backend/backups/ directory.
DB calls are monkeypatched so no live database is required.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Add scripts/ to sys.path so cleanup_backups can be imported as a module.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cleanup_backups  # noqa: E402
from cleanup_backups import (  # noqa: E402
    ACTIVE_STATUSES,
    _dir_size_bytes,
    _format_bytes,
    classify_backups,
)

# Fixed reference timestamps used throughout tests.
_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(days=30)      # clearly older than any default threshold
_RECENT = _NOW - timedelta(days=2)    # newer than the default 14-day threshold


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backup_dir(root: Path, age_dt: datetime | None = None) -> Path:
    """Create a fake backup directory containing a minimal manifest.json."""
    run_id = str(uuid.uuid4())
    d = root / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("[]", encoding="utf-8")
    if age_dt is not None:
        ts = age_dt.timestamp()
        os.utime(d, (ts, ts))
    return d


# ---------------------------------------------------------------------------
# classify_backups — pure-logic tests (no main(), no argv, no real DB)
# ---------------------------------------------------------------------------

def test_classify_eligible_when_old_and_terminal(tmp_path):
    d = _make_backup_dir(tmp_path, _OLD)
    eligible, skipped = classify_backups(
        all_dirs=[d],
        db_statuses={d.name: "complete"},
        older_than_days=14,
        keep_last=None,
        now=_NOW,
    )
    assert d in eligible
    assert skipped == []


def test_classify_running_status_always_protected(tmp_path):
    d = _make_backup_dir(tmp_path, _OLD)
    eligible, skipped = classify_backups(
        all_dirs=[d],
        db_statuses={d.name: "running"},
        older_than_days=0,
        keep_last=None,
        now=_NOW,
    )
    assert d not in eligible
    assert len(skipped) == 1
    assert "active" in skipped[0][1]


def test_classify_all_active_statuses_are_protected(tmp_path):
    for status in ACTIVE_STATUSES:
        d = _make_backup_dir(tmp_path, _OLD)
        eligible, _ = classify_backups(
            all_dirs=[d],
            db_statuses={d.name: status},
            older_than_days=0,
            keep_last=None,
            now=_NOW,
        )
        assert d not in eligible, f"Active status {status!r} was not protected"


def test_classify_skips_too_recent(tmp_path):
    d = _make_backup_dir(tmp_path, _RECENT)
    eligible, skipped = classify_backups(
        all_dirs=[d],
        db_statuses={d.name: "complete"},
        older_than_days=14,
        keep_last=None,
        now=_NOW,
    )
    assert d not in eligible
    assert len(skipped) == 1
    assert "too recent" in skipped[0][1]


def test_classify_unknown_run_id_eligible_by_age(tmp_path):
    d = _make_backup_dir(tmp_path, _OLD)
    # run_id is NOT in db_statuses — treated as unknown (test artifact, old run, etc.)
    eligible, skipped = classify_backups(
        all_dirs=[d],
        db_statuses={},
        older_than_days=14,
        keep_last=None,
        now=_NOW,
    )
    assert d in eligible
    assert skipped == []


def test_classify_keep_last_protects_newest(tmp_path):
    # Create 5 dirs with staggered ages (oldest = most negative days offset).
    dirs = [_make_backup_dir(tmp_path, _OLD - timedelta(days=i * 5)) for i in range(5)]
    # Sorted newest-first: dirs[0] is most recent (only _OLD), dirs[4] is oldest.

    eligible, skipped = classify_backups(
        all_dirs=dirs,
        db_statuses={d.name: "complete" for d in dirs},
        older_than_days=0,   # age=0 so all pass the age filter
        keep_last=3,
        now=_NOW,
    )

    sorted_by_mtime = sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)
    kept_names = {d.name for d in sorted_by_mtime[:3]}
    eligible_names = {d.name for d in eligible}

    # The 3 newest must not be deleted.
    assert eligible_names.isdisjoint(kept_names)
    # The 2 oldest must be eligible.
    oldest_names = {d.name for d in sorted_by_mtime[3:]}
    assert oldest_names.issubset(eligible_names)


def test_classify_keep_last_zero_keeps_nothing_extra(tmp_path):
    d = _make_backup_dir(tmp_path, _OLD)
    eligible, skipped = classify_backups(
        all_dirs=[d],
        db_statuses={d.name: "complete"},
        older_than_days=0,
        keep_last=0,
        now=_NOW,
    )
    assert d in eligible


def test_classify_keep_last_exceeds_count(tmp_path):
    dirs = [_make_backup_dir(tmp_path, _OLD) for _ in range(3)]
    # keep_last=10 with only 3 dirs → all kept, none eligible.
    eligible, skipped = classify_backups(
        all_dirs=dirs,
        db_statuses={d.name: "complete" for d in dirs},
        older_than_days=0,
        keep_last=10,
        now=_NOW,
    )
    assert eligible == []
    assert len(skipped) == 3


def test_classify_active_beats_keep_last(tmp_path):
    d = _make_backup_dir(tmp_path, _OLD)
    # Even with keep_last=0 (keep nothing), active status still protects.
    eligible, skipped = classify_backups(
        all_dirs=[d],
        db_statuses={d.name: "running"},
        older_than_days=0,
        keep_last=0,
        now=_NOW,
    )
    assert d not in eligible
    skip_reasons = [r for _, r in skipped]
    assert any("active" in r for r in skip_reasons)


def test_classify_empty_dir_list(tmp_path):
    eligible, skipped = classify_backups(
        all_dirs=[],
        db_statuses={},
        older_than_days=14,
        keep_last=None,
        now=_NOW,
    )
    assert eligible == []
    assert skipped == []


def test_classify_eligible_oldest_first(tmp_path):
    # Confirm eligible list is returned oldest-first.
    newer = _make_backup_dir(tmp_path, _OLD)
    older = _make_backup_dir(tmp_path, _OLD - timedelta(days=10))
    eligible, _ = classify_backups(
        all_dirs=[newer, older],
        db_statuses={newer.name: "complete", older.name: "complete"},
        older_than_days=0,
        keep_last=None,
        now=_NOW,
    )
    assert eligible[0] == older
    assert eligible[1] == newer


# ---------------------------------------------------------------------------
# main() integration tests — monkeypatch sys.argv and _load_run_statuses
# ---------------------------------------------------------------------------

def _no_db(run_ids):
    return {}


def test_dry_run_deletes_nothing(tmp_path, monkeypatch):
    d = _make_backup_dir(tmp_path, _OLD)
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", _no_db)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "14",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert d.exists(), "Dry-run must not delete anything"


def test_explicit_dry_run_flag_deletes_nothing(tmp_path, monkeypatch):
    d = _make_backup_dir(tmp_path, _OLD)
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", _no_db)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "14",
        "--dry-run",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert d.exists()


def test_delete_removes_old_eligible(tmp_path, monkeypatch):
    old_dir = _make_backup_dir(tmp_path, _OLD)
    recent_dir = _make_backup_dir(tmp_path, _RECENT)

    statuses = {old_dir.name: "complete", recent_dir.name: "complete"}
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", lambda ids: statuses)
    monkeypatch.setattr(cleanup_backups, "_now_utc", lambda: _NOW)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "14",
        "--delete",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert not old_dir.exists(), "Old terminal backup should be deleted"
    assert recent_dir.exists(), "Recent backup should survive"


def test_active_run_protected_in_delete_mode_with_age_zero(tmp_path, monkeypatch):
    d = _make_backup_dir(tmp_path, _OLD)
    monkeypatch.setattr(
        cleanup_backups,
        "_load_run_statuses",
        lambda ids: {d.name: "running"},
    )
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "0",
        "--delete",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert d.exists(), "Active run backup must survive even with --older-than-days 0"


def test_awaiting_approval_protected_in_delete_mode(tmp_path, monkeypatch):
    d = _make_backup_dir(tmp_path, _OLD)
    monkeypatch.setattr(
        cleanup_backups,
        "_load_run_statuses",
        lambda ids: {d.name: "awaiting_final_approval"},
    )
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "0",
        "--delete",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert d.exists()


def test_unknown_run_id_cleaned_by_age(tmp_path, monkeypatch):
    old_dir = _make_backup_dir(tmp_path, _OLD)
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", lambda ids: {})
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "14",
        "--delete",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert not old_dir.exists(), "Unknown old backup should be deleted"


def test_keep_last_in_delete_mode(tmp_path, monkeypatch):
    # 5 dirs, all old, all terminal. keep-last=2 → delete 3, keep 2.
    dirs = [_make_backup_dir(tmp_path, _OLD - timedelta(days=i * 5)) for i in range(5)]
    statuses = {d.name: "complete" for d in dirs}
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", lambda ids: statuses)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "0",
        "--keep-last", "2",
        "--delete",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    surviving = [d for d in dirs if d.exists()]
    deleted = [d for d in dirs if not d.exists()]
    assert len(surviving) == 2
    assert len(deleted) == 3


def test_keep_last_keeps_the_newest_ones(tmp_path, monkeypatch):
    # Confirm it's the NEWEST dirs that survive, not arbitrary ones.
    dirs = [_make_backup_dir(tmp_path, _OLD - timedelta(days=i * 5)) for i in range(5)]
    sorted_by_mtime = sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)
    newest_two = {d.name for d in sorted_by_mtime[:2]}

    statuses = {d.name: "complete" for d in dirs}
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", lambda ids: statuses)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "0",
        "--keep-last", "2",
        "--delete",
    ])
    cleanup_backups.main()

    for d in dirs:
        if d.name in newest_two:
            assert d.exists(), f"Newest dir {d.name} should survive keep-last=2"
        else:
            assert not d.exists(), f"Older dir {d.name} should be deleted"


def test_empty_backup_dir_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", _no_db)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
    ])
    rc = cleanup_backups.main()
    assert rc == 0


def test_nonexistent_backup_dir_exits_cleanly(tmp_path, monkeypatch):
    nonexistent = tmp_path / "does_not_exist"
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(nonexistent),
    ])
    rc = cleanup_backups.main()
    assert rc == 0


def test_invalid_older_than_days_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", _no_db)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(tmp_path),
        "--older-than-days", "-1",
    ])
    rc = cleanup_backups.main()
    assert rc == 1


def test_symlinked_backup_dirs_skipped(tmp_path, monkeypatch):
    """Symlinked backup dirs are skipped — the real directory is never deleted."""
    real_dir = tmp_path / "real_data"
    real_dir.mkdir()
    (real_dir / "important.txt").write_text("keep me", encoding="utf-8")

    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()
    link = backup_root / str(uuid.uuid4())
    try:
        link.symlink_to(real_dir)
    except (OSError, NotImplementedError):
        pytest.skip("Cannot create symlinks on this platform/user combination")

    monkeypatch.setattr(cleanup_backups, "_load_run_statuses", _no_db)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_backups.py",
        "--backup-dir", str(backup_root),
        "--older-than-days", "0",
        "--delete",
        "--verbose",
    ])
    rc = cleanup_backups.main()
    assert rc == 0
    assert real_dir.exists(), "Real directory behind symlink must not be deleted"
    assert (real_dir / "important.txt").exists()


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

def test_format_bytes_units():
    assert _format_bytes(0) == "0 B"
    assert _format_bytes(512) == "512 B"
    assert _format_bytes(1023) == "1023 B"
    assert "KB" in _format_bytes(2048)
    assert "MB" in _format_bytes(2 * 1024 * 1024)


def test_dir_size_bytes_counts_file_content(tmp_path):
    d = tmp_path / "backup"
    d.mkdir()
    (d / "manifest.json").write_text("hello", encoding="utf-8")
    size = _dir_size_bytes(d)
    assert size >= 5   # "hello" is at least 5 bytes


def test_dir_size_bytes_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert _dir_size_bytes(d) == 0


def test_dir_size_bytes_nested_files(tmp_path):
    d = tmp_path / "backup"
    (d / "original" / "src").mkdir(parents=True)
    (d / "manifest.json").write_text("x" * 100, encoding="utf-8")
    (d / "original" / "src" / "app.py").write_text("y" * 200, encoding="utf-8")
    size = _dir_size_bytes(d)
    assert size >= 300
