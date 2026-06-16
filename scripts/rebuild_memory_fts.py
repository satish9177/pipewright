"""
Explicitly rebuild derived project-memory FTS rows for one project.

This is an operator/test entrypoint only. It does not change retrieval behavior,
add runtime triggers, or make the FTS index authoritative.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, replace
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import database  # noqa: E402
from backend.db.database import MEMORY_FTS_TABLE  # noqa: E402
from backend.memory.memory_fts import rebuild_memory_fts  # noqa: E402
from backend.memory.memory_retriever import FTSMemoryRetriever  # noqa: E402
from backend.memory.memory_store import add_fact, compute_content_hash  # noqa: E402
from backend.memory.prompt_builder import (  # noqa: E402
    MANDATORY_CATEGORIES,
    ROLE_CATEGORIES,
    InjectedMemoryEntry,
    RequestContext,
    build_project_memory_block_detailed,
)
from backend.pipeline import policy as pipeline_policy  # noqa: E402


@dataclass(frozen=True)
class EntrySnapshot:
    fact_id: str | None
    content: str
    category: str
    scope: str
    priority: int


@dataclass(frozen=True)
class CompareReport:
    project_id: str
    role: str
    included_set_identical: bool
    mandatory_tier_identical: bool
    relevance_sets_identical: bool
    relevance_order_delta: int
    fts_coverage_count: int
    fallback_count: int
    no_cross_project_facts: bool
    deterministic_output: bool = False

    @property
    def only_relevance_tier_may_reorder(self) -> bool:
        return (
            self.included_set_identical
            and self.mandatory_tier_identical
            and self.relevance_sets_identical
        )


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

    compare = subparsers.add_parser(
        "compare",
        help="Read-only rung-0 vs rung-1 memory ordering comparison",
    )
    compare.add_argument(
        "--project-id",
        help="Existing project id to compare. Required unless --seed is used.",
    )
    compare.add_argument("--role", default="planner", help="Prompt role to compare")
    compare.add_argument(
        "--token-budget",
        type=int,
        help="Optional memory token budget override",
    )
    compare.add_argument("--title", help="RequestContext title")
    compare.add_argument("--description", help="RequestContext description")
    compare.add_argument(
        "--file-expected",
        action="append",
        default=[],
        help="Expected file path for request context; may be repeated",
    )
    compare.add_argument("--steer-text", help="RequestContext steer text")
    compare.add_argument(
        "--seed",
        action="store_true",
        help="Create a throwaway project/corpus, compare it, then clean it up",
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


@contextmanager
def _temporary_fts_retrieval(enabled: bool):
    original = pipeline_policy.MEMORY_FTS_RETRIEVAL_ENABLED
    pipeline_policy.MEMORY_FTS_RETRIEVAL_ENABLED = enabled
    try:
        yield
    finally:
        pipeline_policy.MEMORY_FTS_RETRIEVAL_ENABLED = original


def _snapshot_entry(entry: InjectedMemoryEntry) -> EntrySnapshot:
    return EntrySnapshot(
        fact_id=entry.fact_id,
        content=entry.content,
        category=entry.category,
        scope=entry.scope,
        priority=entry.priority,
    )


def _is_mandatory_entry(entry: EntrySnapshot) -> bool:
    if entry.category in MANDATORY_CATEGORIES:
        return True
    return (
        pipeline_policy.MEMORY_RELEVANCE_OMISSION_ENABLED
        and entry.priority <= pipeline_policy.MEMORY_PIN_PRIORITY_THRESHOLD
    )


def _mandatory_tier(entries: tuple[EntrySnapshot, ...]) -> tuple[EntrySnapshot, ...]:
    tier: list[EntrySnapshot] = []
    for entry in entries:
        if not _is_mandatory_entry(entry):
            break
        tier.append(entry)
    return tuple(tier)


def _relevance_entries(
    entries: tuple[EntrySnapshot, ...],
) -> tuple[EntrySnapshot, ...]:
    return tuple(entry for entry in entries if not _is_mandatory_entry(entry))


def _entry_ids(entries: tuple[EntrySnapshot, ...]) -> tuple[str | None, ...]:
    return tuple(entry.fact_id for entry in entries)


def _order_delta(
    off_entries: tuple[EntrySnapshot, ...],
    on_entries: tuple[EntrySnapshot, ...],
) -> int:
    off_ids = _entry_ids(off_entries)
    on_ids = _entry_ids(on_entries)
    paired_delta = sum(
        1 for off_id, on_id in zip(off_ids, on_ids, strict=False)
        if off_id != on_id
    )
    return paired_delta + abs(len(off_ids) - len(on_ids))


def _request_context_from_args(args: argparse.Namespace) -> RequestContext:
    return RequestContext(
        title=args.title,
        description=args.description,
        files_expected=tuple(args.file_expected or ()),
        steer_text=args.steer_text,
    )


def _included_entries(
    project_id: str,
    *,
    role: str,
    token_budget: int | None,
    request_context: RequestContext,
    fts_enabled: bool,
) -> tuple[EntrySnapshot, ...]:
    with _temporary_fts_retrieval(fts_enabled):
        result = build_project_memory_block_detailed(
            project_id,
            role=role,
            token_budget=token_budget,
            request_context=request_context,
        )
    return tuple(_snapshot_entry(entry) for entry in result.included_entries)


def _no_cross_project_facts(project_id: str, fact_ids: set[str | None]) -> bool:
    ids = {fact_id for fact_id in fact_ids if fact_id}
    if not ids:
        return True
    placeholders = ", ".join(f":id_{index}" for index, _ in enumerate(ids))
    params = {"project_id": project_id}
    params.update({f"id_{index}": fact_id for index, fact_id in enumerate(ids)})
    with database.engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT COUNT(*) FROM memory_facts
            WHERE id IN ({placeholders})
              AND project_id != :project_id
        """), params).fetchone()
    return int(row[0]) == 0 if row is not None else True


def _fts_coverage_count(
    project_id: str,
    *,
    role: str,
    request_context: RequestContext,
) -> int:
    role_key = (role or "default").strip().lower()
    categories = ROLE_CATEGORIES.get(role_key, ROLE_CATEGORIES["default"])
    candidates = FTSMemoryRetriever().retrieve_candidates(
        project_id,
        categories,
        request_context=request_context,
    )
    return 1 if candidates.relevance_scores else 0


def _build_compare_report_once(
    project_id: str,
    *,
    role: str,
    token_budget: int | None,
    request_context: RequestContext,
) -> CompareReport:
    off_entries = _included_entries(
        project_id,
        role=role,
        token_budget=token_budget,
        request_context=request_context,
        fts_enabled=False,
    )
    on_entries = _included_entries(
        project_id,
        role=role,
        token_budget=token_budget,
        request_context=request_context,
        fts_enabled=True,
    )
    off_ids = set(_entry_ids(off_entries))
    on_ids = set(_entry_ids(on_entries))
    off_relevance = _relevance_entries(off_entries)
    on_relevance = _relevance_entries(on_entries)
    coverage_count = _fts_coverage_count(
        project_id,
        role=role,
        request_context=request_context,
    )
    fact_ids = off_ids | on_ids

    return CompareReport(
        project_id=project_id,
        role=role,
        included_set_identical=off_ids == on_ids,
        mandatory_tier_identical=(
            _mandatory_tier(off_entries) == _mandatory_tier(on_entries)
        ),
        relevance_sets_identical=set(_entry_ids(off_relevance)) == set(
            _entry_ids(on_relevance)
        ),
        relevance_order_delta=_order_delta(off_relevance, on_relevance),
        fts_coverage_count=coverage_count,
        fallback_count=0 if coverage_count else 1,
        no_cross_project_facts=_no_cross_project_facts(project_id, fact_ids),
    )


def compare_memory_fts(
    project_id: str,
    *,
    role: str = "planner",
    token_budget: int | None = None,
    request_context: RequestContext | None = None,
) -> CompareReport:
    project_id = str(project_id).strip()
    role = (role or "default").strip().lower()
    request_context = request_context or RequestContext()
    first = _build_compare_report_once(
        project_id,
        role=role,
        token_budget=token_budget,
        request_context=request_context,
    )
    second = _build_compare_report_once(
        project_id,
        role=role,
        token_budget=token_budget,
        request_context=request_context,
    )
    return replace(first, deterministic_output=first == second)


def _memory_fts_table_exists(conn) -> bool:
    row = conn.execute(text("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = :table_name
    """), {"table_name": MEMORY_FTS_TABLE}).fetchone()
    return row is not None


def _insert_seed_suggestion(project_id: str) -> None:
    content = "Seed pending suggestion must be cleaned up"
    with database.engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO memory_suggestions (
                id,
                project_id,
                content,
                category,
                scope,
                priority,
                source,
                evidence_excerpt,
                status,
                content_hash,
                rationale
            )
            VALUES (
                :id,
                :project_id,
                :content,
                'stack',
                'backend',
                100,
                'fts_seed',
                'seed suggestion evidence',
                'pending',
                :content_hash,
                'seed suggestion rationale'
            )
        """), {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "content": content,
            "content_hash": compute_content_hash(content),
        })


def _seed_project() -> str:
    project_id = f"fts-seed-{uuid.uuid4().hex}"
    add_fact(
        project_id,
        "Security seed fact: approval gates stay visible.",
        category="security",
    )
    add_fact(
        project_id,
        "Style seed fact alpha baseline.",
        category="style",
    )
    add_fact(
        project_id,
        "Style seed fact uses Azure storage.",
        category="style",
    )
    add_fact(
        project_id,
        "Test seed fact uses pytest fixtures.",
        category="test",
    )
    _insert_seed_suggestion(project_id)
    rebuild_memory_fts(project_id)
    return project_id


def _cleanup_seed_project(project_id: str) -> None:
    with database.engine.begin() as conn:
        if _memory_fts_table_exists(conn):
            conn.execute(
                text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE project_id = :project_id"),
                {"project_id": project_id},
            )
        conn.execute(
            text("DELETE FROM memory_suggestions WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
        conn.execute(
            text("DELETE FROM memory_facts WHERE project_id = :project_id"),
            {"project_id": project_id},
        )


def _print_compare_report(report: CompareReport) -> None:
    print("mode=compare")
    print(f"project_id={report.project_id}")
    print(f"role={report.role}")
    print(f"included_set_identical={report.included_set_identical}")
    print(f"mandatory_tier_identical={report.mandatory_tier_identical}")
    print(f"only_relevance_tier_may_reorder={report.only_relevance_tier_may_reorder}")
    print(f"relevance_order_delta={report.relevance_order_delta}")
    print(f"fts_coverage_count={report.fts_coverage_count}")
    print(f"fallback_count={report.fallback_count}")
    print(f"no_cross_project_facts={report.no_cross_project_facts}")
    print(f"deterministic_output={report.deterministic_output}")


def _run_compare(args: argparse.Namespace) -> int:
    if args.seed and args.project_id:
        print("[ERROR] --seed creates its own throwaway project; omit --project-id.")
        return 2
    if not args.seed and not args.project_id:
        print("[ERROR] compare requires --project-id unless --seed is used.")
        return 2

    request_context = _request_context_from_args(args)
    if args.seed and not any((
        request_context.title,
        request_context.description,
        request_context.files_expected,
        request_context.steer_text,
    )):
        request_context = RequestContext(title="uses")

    if not _fts5_available():
        print("FTS5 unavailable; compare will report rung-0 fallback.")

    if args.seed:
        database.init_db()
        project_id = _seed_project()
        try:
            report = compare_memory_fts(
                project_id,
                role=args.role,
                token_budget=args.token_budget,
                request_context=request_context,
            )
            _print_compare_report(report)
        finally:
            _cleanup_seed_project(project_id)
        return 0

    report = compare_memory_fts(
        str(args.project_id).strip(),
        role=args.role,
        token_budget=args.token_budget,
        request_context=request_context,
    )
    _print_compare_report(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "compare":
        try:
            return _run_compare(args)
        except ValueError as error:
            print(f"[ERROR] {error}")
            return 2
        except Exception as error:
            print(f"[ERROR] memory FTS compare failed: {error}")
            return 1

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
