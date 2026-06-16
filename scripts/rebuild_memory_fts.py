"""
Explicitly rebuild derived project-memory FTS rows for one project.

This is an operator/test entrypoint only. It does not change retrieval behavior,
add runtime triggers, or make the FTS index authoritative.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import database  # noqa: E402
from backend.memory.memory_fts import rebuild_memory_fts  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild the derived memory FTS index for one project",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rebuild = subparsers.add_parser(
        "rebuild",
        help="Rebuild one project's derived FTS rows",
    )
    rebuild.add_argument(
        "--project-id",
        required=True,
        help="Project id whose memory FTS rows should be rebuilt",
    )
    rebuild.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the mutating rebuild without an interactive prompt",
    )

    return parser.parse_args(argv)


def _confirm_rebuild(project_id: str, *, yes: bool) -> bool:
    if yes:
        return True

    if not sys.stdin.isatty():
        print(
            "[ERROR] Refusing to rebuild memory FTS in non-interactive mode "
            "without --yes."
        )
        return False

    print(
        "[WARN] This will DELETE and rebuild derived memory FTS rows for "
        f"project_id={project_id}."
    )
    response = input("Continue? Type YES: ").strip()
    if response != "YES":
        print("Aborted.")
        return False
    return True


def _fts5_available() -> bool:
    with database.engine.connect() as conn:
        return database._sqlite_fts5_available(conn)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command != "rebuild":
        print(f"[ERROR] Unsupported command: {args.command}")
        return 2

    project_id = str(args.project_id).strip()
    database.init_db()

    if not _fts5_available():
        print(
            "FTS5 unavailable; nothing to populate. "
            f"project_id={project_id} rebuilt_rows=0"
        )
        return 0

    if not _confirm_rebuild(project_id, yes=bool(args.yes)):
        return 2

    try:
        rebuilt_rows = rebuild_memory_fts(project_id)
    except ValueError as error:
        print(f"[ERROR] {error}")
        return 2
    except Exception as error:
        print(f"[ERROR] memory FTS rebuild failed: {error}")
        return 1

    print(f"project_id={project_id} rebuilt_rows={rebuilt_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
