"""
cleanup_backups.py
Manual cleanup tool for backend/backups/ — the patch-rollback scratch space.

Safe: dry-run by default. Never deletes outside the backup root.
DB-aware: protects backups for non-terminal pipeline runs.

Usage:
  python scripts/cleanup_backups.py               # dry-run, show eligible
  python scripts/cleanup_backups.py --delete      # delete eligible backups
  python scripts/cleanup_backups.py --older-than-days 30 --delete
  python scripts/cleanup_backups.py --keep-last 20 --delete
  python scripts/cleanup_backups.py --older-than-days 0 --delete --verbose
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.statuses import RunStatus

# Computed from the script location so importing this module does not pull in
# the full patch_applier dependency graph (checkpoint_store, project_context, …).
_DEFAULT_BACKUP_DIR: Path = ROOT / "backend" / "backups"

# Non-terminal statuses — backups for these run_ids are always kept.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    RunStatus.RUNNING,
    RunStatus.RUNNING_CHUNKS,
    RunStatus.STARTED,
    RunStatus.INTERRUPTED,
    RunStatus.PAUSED,
    RunStatus.AWAITING_CHUNK_PLAN_APPROVAL,
    RunStatus.CHUNK_PLAN_APPROVED,
    RunStatus.AWAITING_CHUNK_APPROVAL,
    RunStatus.CHUNK_APPROVED,
    RunStatus.AWAITING_FINAL_APPROVAL,
    RunStatus.FINAL_APPROVED,
    RunStatus.PUSHING,
})


# ---------------------------------------------------------------------------
# Pure helpers — no backend imports, fully testable in isolation.
# ---------------------------------------------------------------------------

def _dir_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file() and not f.is_symlink():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def classify_backups(
    all_dirs: list[Path],
    db_statuses: dict[str, str],
    older_than_days: int,
    keep_last: int | None,
    now: datetime,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """
    Classify backup directories into eligible-for-deletion and skipped.

    Rules applied in order:
      1. Active DB status → always skip (protected).
      2. In the keep-last set → skip.
      3. Modified within the age threshold → skip (too recent).
      4. Everything else → eligible.

    Returns:
        eligible: list of Path objects safe to delete, oldest-first.
        skipped:  list of (Path, reason) pairs.
    """
    cutoff = now - timedelta(days=older_than_days)

    # Sort by mtime descending (most recent first) for keep-last selection.
    sorted_dirs = sorted(all_dirs, key=_dir_mtime, reverse=True)

    keep_last_names: set[str] = set()
    if keep_last is not None:
        for d in sorted_dirs[:keep_last]:
            keep_last_names.add(d.name)

    eligible: list[Path] = []
    skipped: list[tuple[Path, str]] = []

    for entry in sorted_dirs:
        db_status = db_statuses.get(entry.name)

        if db_status is not None and db_status in ACTIVE_STATUSES:
            skipped.append((entry, f"active ({db_status})"))
            continue

        if entry.name in keep_last_names:
            skipped.append((entry, "keep-last"))
            continue

        mtime_dt = datetime.fromtimestamp(_dir_mtime(entry), tz=timezone.utc)
        age_days = (now - mtime_dt).days
        if mtime_dt > cutoff:
            skipped.append((entry, f"too recent ({age_days}d < {older_than_days}d threshold)"))
            continue

        eligible.append(entry)

    # Return eligible oldest-first so output reads naturally.
    eligible.reverse()
    return eligible, skipped


def _collect_top_level_dirs(backup_root: Path, verbose: bool) -> list[Path]:
    """Return top-level directories inside backup_root, skipping symlinks and non-dirs."""
    result: list[Path] = []
    try:
        entries = list(backup_root.iterdir())
    except OSError as exc:
        print(f"[ERROR] Cannot list backup directory: {exc}")
        return result

    for entry in entries:
        if entry.is_symlink():
            if verbose:
                print(f"  [SKIP] {entry.name} — symlink, skipping for safety")
            continue
        if not entry.is_dir():
            if verbose:
                print(f"  [SKIP] {entry.name} — not a directory")
            continue
        # Belt-and-suspenders: ensure resolved path stays inside backup_root.
        try:
            entry.resolve().relative_to(backup_root.resolve())
        except ValueError:
            if verbose:
                print(f"  [SKIP] {entry.name} — resolved path is outside backup root")
            continue
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# DB query — deferred import so module-level import stays lightweight.
# ---------------------------------------------------------------------------

def _load_run_statuses(run_ids: list[str]) -> dict[str, str]:
    """
    Query pipeline_runs for the given run_ids. Returns {} on any error.
    The caller treats missing run_ids as unknown (eligible by age).
    """
    if not run_ids:
        return {}
    try:
        from backend.db.database import engine, init_db
        from sqlalchemy import text
        init_db()
        placeholders = ", ".join(f":id{i}" for i in range(len(run_ids)))
        params = {f"id{i}": run_id for i, run_id in enumerate(run_ids)}
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT id, status FROM pipeline_runs"
                    f" WHERE id IN ({placeholders})"
                ),
                params,
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    except Exception as exc:
        print(f"[WARN] Could not query DB for run statuses: {exc}")
        print("[WARN] Treating all backup dirs as unknown (no DB protection active).")
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean up old patch-rollback backups from backend/backups/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python scripts/cleanup_backups.py
  python scripts/cleanup_backups.py --delete
  python scripts/cleanup_backups.py --older-than-days 30 --delete
  python scripts/cleanup_backups.py --keep-last 20 --delete
  python scripts/cleanup_backups.py --older-than-days 0 --delete --verbose
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be deleted without deleting (default unless --delete).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        default=False,
        help="Actually delete eligible backup directories.",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=14,
        metavar="N",
        dest="older_than_days",
        help="Only delete backups last modified more than N days ago (default: 14).",
    )
    parser.add_argument(
        "--keep-last",
        type=int,
        default=None,
        metavar="N",
        dest="keep_last",
        help="Always keep the N most recently modified backup directories.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Print details for each directory, including skipped ones.",
    )
    parser.add_argument(
        "--backup-dir",
        type=str,
        default=None,
        metavar="PATH",
        dest="backup_dir",
        help="Override the backup root directory (default: backend/backups/).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    is_delete = args.delete

    if args.older_than_days < 0:
        print("[ERROR] --older-than-days must be >= 0")
        return 1
    if args.keep_last is not None and args.keep_last < 0:
        print("[ERROR] --keep-last must be >= 0")
        return 1

    backup_root = (
        Path(args.backup_dir).resolve()
        if args.backup_dir is not None
        else _DEFAULT_BACKUP_DIR.resolve()
    )

    if not backup_root.exists():
        print(f"Backup directory does not exist: {backup_root}")
        print("Nothing to clean.")
        return 0

    if not backup_root.is_dir():
        print(f"[ERROR] Backup path is not a directory: {backup_root}")
        return 1

    if is_delete and args.older_than_days == 0 and args.keep_last is None:
        print(
            "[WARNING] --older-than-days 0 with --delete removes ALL non-active "
            "backups, including recent debug/rollback material."
        )

    all_dirs = _collect_top_level_dirs(backup_root, args.verbose)

    if not all_dirs:
        print("No backup directories found.")
        return 0

    run_ids = [d.name for d in all_dirs]
    db_statuses = _load_run_statuses(run_ids)

    now = datetime.now(timezone.utc)
    eligible, skipped = classify_backups(
        all_dirs=all_dirs,
        db_statuses=db_statuses,
        older_than_days=args.older_than_days,
        keep_last=args.keep_last,
        now=now,
    )

    total_size = sum(_dir_size_bytes(d) for d in eligible)
    count = len(eligible)
    noun = "directory" if count == 1 else "directories"

    if not is_delete:
        if eligible:
            print(
                f"[DRY RUN] Would delete {count} backup {noun}"
                f" ({_format_bytes(total_size)})"
            )
            for entry in eligible:
                db_status = db_statuses.get(entry.name, "not in DB")
                mtime = datetime.fromtimestamp(_dir_mtime(entry), tz=timezone.utc)
                size = _dir_size_bytes(entry)
                print(
                    f"  {entry.name}"
                    f" | modified {mtime.strftime('%Y-%m-%d')}"
                    f" | status: {db_status}"
                    f" | {_format_bytes(size)}"
                )
            print("Run with --delete to remove eligible directories.")
        else:
            print("[DRY RUN] No eligible backup directories found.")
        if args.verbose and skipped:
            skip_noun = "directory" if len(skipped) == 1 else "directories"
            print(f"\nSkipped {len(skipped)} {skip_noun}:")
            for p, reason in skipped:
                print(f"  [SKIP] {p.name} — {reason}")
        return 0

    # Delete mode.
    deleted_count = 0
    deleted_size = 0
    errors = 0
    for entry in eligible:
        db_status = db_statuses.get(entry.name, "not in DB")
        size = _dir_size_bytes(entry)
        mtime = datetime.fromtimestamp(_dir_mtime(entry), tz=timezone.utc)
        # Final per-entry safety check before deletion.
        try:
            entry.resolve().relative_to(backup_root)
        except ValueError:
            print(f"  [SKIP] {entry.name} — final safety check failed, not deleting")
            errors += 1
            continue
        try:
            shutil.rmtree(entry)
            deleted_count += 1
            deleted_size += size
            if args.verbose:
                print(
                    f"  deleted {entry.name}"
                    f" | modified {mtime.strftime('%Y-%m-%d')}"
                    f" | status: {db_status}"
                    f" | {_format_bytes(size)}"
                )
        except Exception as exc:
            print(f"  [ERROR] Failed to delete {entry.name}: {exc}")
            errors += 1

    del_noun = "directory" if deleted_count == 1 else "directories"
    print(
        f"[DELETE] Removed {deleted_count} backup {del_noun}"
        f" ({_format_bytes(deleted_size)} freed)"
    )
    if errors:
        print(f"[WARN] {errors} error(s) during deletion — see above.")
    if args.verbose and skipped:
        skip_noun = "directory" if len(skipped) == 1 else "directories"
        print(f"\nSkipped {len(skipped)} {skip_noun} (protected or kept):")
        for p, reason in skipped:
            print(f"  [SKIP] {p.name} — {reason}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
