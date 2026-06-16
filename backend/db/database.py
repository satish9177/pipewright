"""
database.py
SQLAlchemy engine and session setup.
All operations are synchronous.
Database file lives at backend/db/pipewright.db
"""

import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from pathlib import Path

DB_DIR = Path(__file__).parent

# DB location is resolved once, at import time, because the engine below is
# bound to it immediately and is captured by reference across the codebase
# (`from backend.db.database import engine`). PIPEWRIGHT_DB_PATH lets callers
# point at a different SQLite file (e.g. a Docker volume, or an isolated
# per-session test DB) without touching the real local app DB. When it is
# unset, the default backend/db/pipewright.db is used and behavior is unchanged.
_DB_PATH_OVERRIDE = os.environ.get("PIPEWRIGHT_DB_PATH")
if _DB_PATH_OVERRIDE:
    DB_PATH = Path(_DB_PATH_OVERRIDE)
    # The override may point somewhere that does not exist yet; SQLite will not
    # create missing parent directories on its own.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
else:
    DB_PATH = DB_DIR / "pipewright.db"

SCHEMA_PATH = DB_DIR / "schema.sql"
MEMORY_FTS_TABLE = "memory_facts_fts"
MEMORY_FTS_COLUMNS = (
    "content",
    "fact_id",
    "project_id",
    "category",
    "scope",
    "content_hash",
)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False
)

# Local SQLite reliability (#32D). Pipewright's local/open-source default is a
# single SQLite file accessed by the API thread and offloaded worker threads
# (#32C). Under that concurrency SQLite's default rollback journal + zero busy
# timeout surfaces "database is locked" errors. WAL lets readers and a writer
# proceed concurrently, and busy_timeout makes a contended writer wait briefly
# instead of erroring immediately.
#
# This is applied per new DBAPI connection via the SQLAlchemy "connect" event.
# It is intentionally NOT a schema or migration change: journal_mode=WAL is a
# property of the database file and busy_timeout is per-connection, so this is
# idempotent and safe for existing DBs. PRAGMAs are scoped to SQLite only.
SQLITE_BUSY_TIMEOUT_MS = 5000


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    # Guard so SQLite-specific PRAGMAs never run against a non-SQLite backend
    # (e.g. a future PostgreSQL hosted/team path).
    if engine.dialect.name != "sqlite":
        return
    # Run on the raw DBAPI connection before any transaction begins; WAL cannot
    # be set inside a transaction. For in-memory SQLite, journal_mode=WAL is a
    # graceful no-op (it stays "memory") rather than an error.
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


class Base(DeclarativeBase):
    pass


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return any(row._mapping["name"] == column_name for row in rows)
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to inspect table {table_name}: {error}"
        )


def _add_column_if_missing(
    conn,
    table_name: str,
    column_name: str,
    migration_sql: str
) -> None:
    try:
        if not _table_exists(conn, table_name):
            return
        if not _column_exists(conn, table_name, column_name):
            conn.execute(text(migration_sql))
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to add column {table_name}.{column_name}: "
            f"{error}"
        )


def _create_index_if_columns_exist(
    conn,
    table_name: str,
    index_sql: str,
    columns: tuple[str, ...],
) -> None:
    if not _table_exists(conn, table_name):
        return
    if all(_column_exists(conn, table_name, column) for column in columns):
        conn.execute(text(index_sql))


def _sqlite_fts5_available(conn) -> bool:
    """
    Return whether this SQLite connection can create FTS5 virtual tables.

    The probe is isolated in a savepoint so an FTS5-missing build no-ops without
    poisoning the surrounding migration transaction.
    """
    if getattr(conn.dialect, "name", None) != "sqlite":
        return False

    try:
        conn.execute(text("SAVEPOINT pipewright_fts5_probe"))
        conn.execute(text("""
            CREATE VIRTUAL TABLE temp.pipewright_fts5_probe
            USING fts5(content)
        """))
        conn.execute(text("DROP TABLE temp.pipewright_fts5_probe"))
        conn.execute(text("RELEASE SAVEPOINT pipewright_fts5_probe"))
        return True
    except Exception:
        try:
            conn.execute(text("ROLLBACK TO SAVEPOINT pipewright_fts5_probe"))
        except Exception:
            pass
        try:
            conn.execute(text("RELEASE SAVEPOINT pipewright_fts5_probe"))
        except Exception:
            pass
        return False


def _memory_fts_shape_matches(conn) -> bool:
    table_sql = (_table_sql(conn, MEMORY_FTS_TABLE) or "").lower()
    if "using fts5" not in table_sql:
        return False

    rows = conn.execute(text(f"PRAGMA table_info({MEMORY_FTS_TABLE})")).fetchall()
    columns = {row._mapping["name"] for row in rows}
    return columns == set(MEMORY_FTS_COLUMNS)


def _create_memory_fts_table(conn) -> None:
    conn.execute(text(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {MEMORY_FTS_TABLE}
        USING fts5(
            content,
            fact_id UNINDEXED,
            project_id UNINDEXED,
            category UNINDEXED,
            scope UNINDEXED,
            content_hash UNINDEXED
        )
    """))


def _ensure_memory_fts_shape(conn) -> None:
    """
    Ensure the derived, rebuildable memory FTS5 table exists when supported.

    This table is dormant scaffold only. It is never a source of truth and is not
    read by prompt construction or runtime memory selection in this slice.
    """
    try:
        if not _sqlite_fts5_available(conn):
            return

        if _table_exists(conn, MEMORY_FTS_TABLE) and not _memory_fts_shape_matches(conn):
            conn.execute(text(f"DROP TABLE {MEMORY_FTS_TABLE}"))

        _create_memory_fts_table(conn)
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to ensure {MEMORY_FTS_TABLE} shape: {error}"
        ) from error


def _migrate_db(conn) -> None:
    """
    Apply small SQLite migrations for existing local databases.
    Safe to call multiple times.
    """
    try:
        migrations = [
            (
                "memory_facts",
                "project_id",
                "ALTER TABLE memory_facts ADD COLUMN project_id TEXT",
            ),
            (
                "memory_facts",
                "category",
                "ALTER TABLE memory_facts ADD COLUMN category TEXT DEFAULT 'other'",
            ),
            (
                "memory_facts",
                "scope",
                "ALTER TABLE memory_facts ADD COLUMN scope TEXT DEFAULT 'global'",
            ),
            (
                "memory_facts",
                "priority",
                "ALTER TABLE memory_facts ADD COLUMN priority INTEGER DEFAULT 100",
            ),
            (
                "memory_facts",
                "approved_by",
                "ALTER TABLE memory_facts ADD COLUMN approved_by TEXT",
            ),
            (
                "memory_facts",
                "approved_at",
                "ALTER TABLE memory_facts ADD COLUMN approved_at DATETIME",
            ),
            (
                "memory_facts",
                "last_verified_at",
                "ALTER TABLE memory_facts ADD COLUMN last_verified_at DATETIME",
            ),
            (
                "memory_facts",
                "content_hash",
                "ALTER TABLE memory_facts ADD COLUMN content_hash TEXT",
            ),
            (
                "memory_facts",
                "superseded_by_fact_id",
                "ALTER TABLE memory_facts ADD COLUMN superseded_by_fact_id TEXT",
            ),
            (
                "pipeline_runs",
                "project_id",
                "ALTER TABLE pipeline_runs ADD COLUMN project_id TEXT",
            ),
            (
                "pipeline_runs",
                "report_json",
                "ALTER TABLE pipeline_runs ADD COLUMN report_json TEXT",
            ),
            (
                "pipeline_runs",
                "chunk_plan_status",
                "ALTER TABLE pipeline_runs ADD COLUMN chunk_plan_status TEXT "
                "DEFAULT 'none'",
            ),
            (
                "pipeline_runs",
                "intent",
                "ALTER TABLE pipeline_runs ADD COLUMN intent TEXT "
                "DEFAULT 'implementation'",
            ),
            (
                "pipeline_runs",
                "chunk_plan",
                "ALTER TABLE pipeline_runs ADD COLUMN chunk_plan TEXT",
            ),
            (
                "pipeline_runs",
                "total_chunks",
                "ALTER TABLE pipeline_runs ADD COLUMN total_chunks INTEGER DEFAULT 0",
            ),
            (
                "pipeline_runs",
                "current_chunk_number",
                "ALTER TABLE pipeline_runs ADD COLUMN current_chunk_number "
                "INTEGER DEFAULT 0",
            ),
            (
                "pipeline_runs",
                "pr_url",
                "ALTER TABLE pipeline_runs ADD COLUMN pr_url TEXT",
            ),
            (
                "pipeline_runs",
                "pr_number",
                "ALTER TABLE pipeline_runs ADD COLUMN pr_number INTEGER",
            ),
            (
                "pipeline_runs",
                "branch_name",
                "ALTER TABLE pipeline_runs ADD COLUMN branch_name TEXT",
            ),
            (
                "pipeline_runs",
                "pushed_at",
                "ALTER TABLE pipeline_runs ADD COLUMN pushed_at DATETIME",
            ),
            (
                "pipeline_runs",
                "pr_created_at",
                "ALTER TABLE pipeline_runs ADD COLUMN pr_created_at DATETIME",
            ),
            (
                "pipeline_runs",
                "push_error",
                "ALTER TABLE pipeline_runs ADD COLUMN push_error TEXT",
            ),
            (
                "pipeline_runs",
                "source_plan_run_id",
                "ALTER TABLE pipeline_runs ADD COLUMN source_plan_run_id TEXT",
            ),
            (
                "pipeline_runs",
                "start_branch",
                "ALTER TABLE pipeline_runs ADD COLUMN start_branch TEXT",
            ),
            (
                "pipeline_runs",
                "start_head_sha",
                "ALTER TABLE pipeline_runs ADD COLUMN start_head_sha TEXT",
            ),
            (
                "pipeline_runs",
                "approved_plan_version",
                "ALTER TABLE pipeline_runs ADD COLUMN approved_plan_version INTEGER",
            ),
            (
                "projects",
                "branch",
                "ALTER TABLE projects ADD COLUMN branch TEXT DEFAULT 'main'",
            ),
            (
                "projects",
                "description",
                "ALTER TABLE projects ADD COLUMN description TEXT DEFAULT ''",
            ),
            (
                "projects",
                "github_token",
                "ALTER TABLE projects ADD COLUMN github_token TEXT",
            ),
            (
                "projects",
                "github_owner",
                "ALTER TABLE projects ADD COLUMN github_owner TEXT",
            ),
            (
                "projects",
                "github_repo",
                "ALTER TABLE projects ADD COLUMN github_repo TEXT",
            ),
            (
                "projects",
                "github_base_branch",
                "ALTER TABLE projects ADD COLUMN github_base_branch TEXT "
                "DEFAULT 'pipewright-staging'",
            ),
            (
                # No DEFAULT here on purpose: existing rows stay NULL so the
                # backfill below can distinguish them and choose manual_token
                # vs local_only. Fresh DBs get the default from schema.sql and
                # the project store always sets pr_mode explicitly on insert.
                "projects",
                "pr_mode",
                "ALTER TABLE projects ADD COLUMN pr_mode TEXT",
            ),
            (
                "checkpoints",
                "chunk_number",
                "ALTER TABLE checkpoints ADD COLUMN chunk_number INTEGER DEFAULT 0",
            ),
            (
                "checkpoints",
                "step_completed",
                "ALTER TABLE checkpoints ADD COLUMN step_completed INTEGER DEFAULT 1",
            ),
            (
                "approval_gates",
                "created_at",
                "ALTER TABLE approval_gates ADD COLUMN created_at DATETIME",
            ),
            (
                "approval_gates",
                "chunk_number",
                "ALTER TABLE approval_gates ADD COLUMN chunk_number INTEGER DEFAULT 0",
            ),
            (
                "approval_gates",
                "approval_type",
                "ALTER TABLE approval_gates ADD COLUMN approval_type TEXT DEFAULT 'legacy'",
            ),
            (
                # Display-only runtime test-validation evidence (#28D). Nullable,
                # no DEFAULT: existing chunks load as NULL and are never gated.
                "chunks",
                "test_run_verdict",
                "ALTER TABLE chunks ADD COLUMN test_run_verdict TEXT",
            ),
            (
                "chunks",
                "test_run_verdict_reason",
                "ALTER TABLE chunks ADD COLUMN test_run_verdict_reason TEXT",
            ),
            (
                "chunks",
                "test_run_command_quality",
                "ALTER TABLE chunks ADD COLUMN test_run_command_quality TEXT",
            ),
            (
                "chunks",
                "test_run_counts_parsed",
                "ALTER TABLE chunks ADD COLUMN test_run_counts_parsed INTEGER",
            ),
            (
                "chunks",
                "test_run_zero_tests_detected",
                "ALTER TABLE chunks ADD COLUMN test_run_zero_tests_detected INTEGER",
            ),
            (
                "chunks",
                "test_run_counts_json",
                "ALTER TABLE chunks ADD COLUMN test_run_counts_json TEXT",
            ),
            (
                "memory_suggestions",
                "source_run_id",
                "ALTER TABLE memory_suggestions ADD COLUMN source_run_id TEXT",
            ),
            (
                "memory_suggestions",
                "source_chunk_number",
                "ALTER TABLE memory_suggestions ADD COLUMN source_chunk_number INTEGER",
            ),
            (
                "memory_suggestions",
                "source_type",
                "ALTER TABLE memory_suggestions ADD COLUMN source_type TEXT",
            ),
            (
                "memory_suggestions",
                "source_ref",
                "ALTER TABLE memory_suggestions ADD COLUMN source_ref TEXT",
            ),
            (
                "memory_suggestions",
                "rationale",
                "ALTER TABLE memory_suggestions ADD COLUMN rationale TEXT",
            ),
            (
                "memory_suggestions",
                "suggested_by",
                "ALTER TABLE memory_suggestions ADD COLUMN suggested_by TEXT",
            ),
            (
                "memory_suggestions",
                "risk_level",
                "ALTER TABLE memory_suggestions ADD COLUMN risk_level TEXT",
            ),
            (
                "memory_suggestions",
                "quality_score",
                "ALTER TABLE memory_suggestions ADD COLUMN quality_score INTEGER",
            ),
            (
                "memory_suggestions",
                "edited_content",
                "ALTER TABLE memory_suggestions ADD COLUMN edited_content TEXT",
            ),
            (
                "memory_suggestions",
                "approved_fact_id",
                "ALTER TABLE memory_suggestions ADD COLUMN approved_fact_id TEXT",
            ),
        ]

        for table_name, column_name, migration_sql in migrations:
            _add_column_if_missing(conn, table_name, column_name, migration_sql)
        if _column_exists(conn, "approval_gates", "created_at"):
            conn.execute(text("""
                UPDATE approval_gates
                SET created_at = CURRENT_TIMESTAMP
                WHERE created_at IS NULL
            """))
        if _column_exists(conn, "approval_gates", "chunk_number"):
            conn.execute(text("""
                UPDATE approval_gates
                SET chunk_number = 0
                WHERE chunk_number IS NULL
            """))
        if _column_exists(conn, "approval_gates", "approval_type"):
            conn.execute(text("""
                UPDATE approval_gates
                SET approval_type = 'legacy'
                WHERE approval_type IS NULL
            """))
        if _column_exists(conn, "projects", "pr_mode"):
            # One-time backfill: existing projects with a full manual GitHub
            # config become manual_token; everything else becomes local_only.
            # Idempotent because it only touches rows still NULL.
            conn.execute(text("""
                UPDATE projects
                SET pr_mode = 'manual_token'
                WHERE pr_mode IS NULL
                  AND github_token IS NOT NULL AND TRIM(github_token) != ''
                  AND github_owner IS NOT NULL AND TRIM(github_owner) != ''
                  AND github_repo IS NOT NULL AND TRIM(github_repo) != ''
            """))
            conn.execute(text("""
                UPDATE projects
                SET pr_mode = 'local_only'
                WHERE pr_mode IS NULL
            """))
        if _column_exists(conn, "memory_facts", "project_id"):
            conn.execute(text("""
                UPDATE memory_facts
                SET status = 'archived',
                    is_stale = 1,
                    archived_reason = COALESCE(
                        archived_reason,
                        'pre-M1 unscoped memory archived for project-safety'
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE project_id IS NULL
                  AND status = 'active'
            """))
        _create_index_if_columns_exist(
            conn,
            "pipeline_runs",
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_project "
            "ON pipeline_runs(project_id)",
            ("project_id",),
        )
        _create_index_if_columns_exist(
            conn,
            "pipeline_runs",
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status "
            "ON pipeline_runs(status)",
            ("status",),
        )
        _create_index_if_columns_exist(
            conn,
            "pipeline_runs",
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_source_plan "
            "ON pipeline_runs(source_plan_run_id)",
            ("source_plan_run_id",),
        )
        _create_index_if_columns_exist(
            conn,
            "chunks",
            "CREATE INDEX IF NOT EXISTS idx_chunks_run_status "
            "ON chunks(run_id, status)",
            ("run_id", "status"),
        )
        _create_index_if_columns_exist(
            conn,
            "approval_gates",
            "CREATE INDEX IF NOT EXISTS idx_approval_gates_run_status "
            "ON approval_gates(run_id, approval_type, status)",
            ("run_id", "approval_type", "status"),
        )
        _create_index_if_columns_exist(
            conn,
            "memory_facts",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_facts_active_dedupe "
            "ON memory_facts(project_id, content_hash) "
            "WHERE status = 'active'",
            ("project_id", "content_hash", "status"),
        )
        _create_index_if_columns_exist(
            conn,
            "memory_facts",
            "CREATE INDEX IF NOT EXISTS idx_memory_facts_project_status "
            "ON memory_facts(project_id, status)",
            ("project_id", "status"),
        )
        _create_index_if_columns_exist(
            conn,
            "memory_suggestions",
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_memory_suggestions_pending_dedupe "
            "ON memory_suggestions(project_id, content_hash) "
            "WHERE status = 'pending'",
            ("project_id", "content_hash", "status"),
        )
        _create_index_if_columns_exist(
            conn,
            "memory_suggestions",
            "CREATE INDEX IF NOT EXISTS idx_memory_suggestions_project_status "
            "ON memory_suggestions(project_id, status)",
            ("project_id", "status"),
        )
        _ensure_memory_fts_shape(conn)
        _ensure_plan_versions_shape(conn)
        _ensure_chunk_attempts_shape(conn)
        _ensure_run_turns_shape(conn)
        _ensure_review_finding_acknowledgements_shape(conn)
        _ensure_project_index_fingerprints_shape(conn)
        _ensure_file_index_shape(conn)
    except Exception as error:
        raise RuntimeError(f"database.py: Failed to run migrations: {error}")


def _table_exists(conn, table_name: str) -> bool:
    try:
        row = conn.execute(text("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
        """), {"table_name": table_name}).fetchone()
        return row is not None
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to inspect table {table_name}: {error}"
        )


def _table_sql(conn, table_name: str) -> str | None:
    try:
        row = conn.execute(text("""
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
        """), {"table_name": table_name}).fetchone()
        return row[0] if row is not None else None
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to inspect table SQL {table_name}: {error}"
        )


def _create_plan_versions_table(conn, table_name: str, *, if_not_exists: bool) -> None:
    clause = "IF NOT EXISTS " if if_not_exists else ""
    conn.execute(text(f"""
        CREATE TABLE {clause}{table_name} (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            triage_json TEXT NOT NULL,
            source TEXT NOT NULL CHECK (
                source IN ('initial', 'seeded', 'plan_turn')
            ),
            created_from_turn_id TEXT,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
        )
    """))


def _plan_versions_source_check_blocks_plan_turn(conn) -> bool:
    table_sql = _table_sql(conn, "plan_versions")
    if not table_sql:
        return False
    lowered = table_sql.lower()
    return "check" in lowered and "plan_turn" not in lowered


def _rebuild_plan_versions_for_plan_turn_source(conn) -> None:
    """
    Rebuild plan_versions only when an existing SQLite CHECK blocks plan_turn.

    SQLite cannot ALTER a CHECK constraint in place. The copy-then-swap keeps
    existing v1 rows intact and lets the transaction roll back to the original
    table if copying into the expanded shape fails.
    """
    conn.execute(text("DROP TABLE IF EXISTS plan_versions__new"))
    _create_plan_versions_table(conn, "plan_versions__new", if_not_exists=False)
    conn.execute(text("""
        INSERT INTO plan_versions__new (
            id,
            run_id,
            version,
            triage_json,
            source,
            created_from_turn_id,
            created_at
        )
        SELECT
            id,
            run_id,
            version,
            triage_json,
            source,
            created_from_turn_id,
            created_at
        FROM plan_versions
    """))
    conn.execute(text("DROP TABLE plan_versions"))
    conn.execute(text("ALTER TABLE plan_versions__new RENAME TO plan_versions"))


def _ensure_plan_versions_shape(conn) -> None:
    """
    Ensure the append-only plan_versions scaffold exists for existing DB files.

    Additive-first: CREATE TABLE IF NOT EXISTS plus indexes. Existing rows are
    preserved if SQLite needs a copy-then-swap rebuild to widen the source CHECK
    for plan_turn. This table records plan JSON already stored in
    pipeline_runs.chunk_plan; it is not a runtime authority in this slice.
    """
    _create_plan_versions_table(conn, "plan_versions", if_not_exists=True)
    if _plan_versions_source_check_blocks_plan_turn(conn):
        _rebuild_plan_versions_for_plan_turn_source(conn)
    _create_index_if_columns_exist(
        conn,
        "plan_versions",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_versions_run_version "
        "ON plan_versions(run_id, version)",
        ("run_id", "version"),
    )


def _ensure_chunk_attempts_shape(conn) -> None:
    """
    Ensure the append-only chunk_attempts ledger exists for existing DB files.

    The table is additive and metadata-only. It is safe to create on every
    startup and does not backfill older runs; missing rows are meaningful to the
    resume path and degrade to legacy behavior.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS chunk_attempts (
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
            trivial_profile_eligible INTEGER,
            head_sha TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES pipeline_runs(id),
            UNIQUE(run_id, chunk_number, attempt_number)
        )
    """))
    _add_column_if_missing(
        conn,
        "chunk_attempts",
        "stage_profile",
        "ALTER TABLE chunk_attempts ADD COLUMN stage_profile TEXT",
    )
    _add_column_if_missing(
        conn,
        "chunk_attempts",
        "trivial_profile_eligible",
        "ALTER TABLE chunk_attempts ADD COLUMN trivial_profile_eligible INTEGER",
    )
    _create_index_if_columns_exist(
        conn,
        "chunk_attempts",
        "CREATE INDEX IF NOT EXISTS idx_chunk_attempts_run_chunk "
        "ON chunk_attempts(run_id, chunk_number, attempt_number)",
        ("run_id", "chunk_number", "attempt_number"),
    )
    _create_index_if_columns_exist(
        conn,
        "chunk_attempts",
        "CREATE INDEX IF NOT EXISTS idx_chunk_attempts_completed_head "
        "ON chunk_attempts(run_id, final_status, chunk_number)",
        ("run_id", "final_status", "chunk_number"),
    )


def _ensure_run_turns_shape(conn) -> None:
    """
    Ensure the append-only run_turns conversation log exists for existing DB
    files (Phase 3 item 13).

    Additive only: CREATE TABLE IF NOT EXISTS, never DROP or rewrite, no
    backfill. The table stores user steer text and metadata only; rows are
    never updated or deleted by the application. Safe on every startup.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS run_turns (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            target_type TEXT NOT NULL DEFAULT 'chunk',
            chunk_number INTEGER NOT NULL,
            steer_text TEXT NOT NULL,
            attempt_id TEXT,
            outcome TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES pipeline_runs(id),
            UNIQUE(run_id, turn_number)
        )
    """))
    _add_column_if_missing(
        conn,
        "run_turns",
        "target_type",
        "ALTER TABLE run_turns ADD COLUMN target_type TEXT NOT NULL "
        "DEFAULT 'chunk'",
    )
    _create_index_if_columns_exist(
        conn,
        "run_turns",
        "CREATE INDEX IF NOT EXISTS idx_run_turns_run_chunk "
        "ON run_turns(run_id, chunk_number, turn_number)",
        ("run_id", "chunk_number", "turn_number"),
    )


def _ensure_review_finding_acknowledgements_shape(conn) -> None:
    """
    Ensure the review-finding acknowledgement table exists for existing DBs.

    Additive only and metadata-only. Older runs have no rows; missing rows mean
    "not acknowledged" only when a current delivered A1 finding exists.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS review_finding_acknowledgements (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            chunk_number INTEGER NOT NULL,
            acknowledged_diff_hash TEXT NOT NULL,
            acknowledged_by TEXT,
            acknowledged_at DATETIME,
            reason TEXT,
            status TEXT DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
        )
    """))
    _create_index_if_columns_exist(
        conn,
        "review_finding_acknowledgements",
        "CREATE INDEX IF NOT EXISTS idx_review_finding_ack_run_chunk_status "
        "ON review_finding_acknowledgements(run_id, chunk_number, status)",
        ("run_id", "chunk_number", "status"),
    )


def _ensure_file_index_shape(conn) -> None:
    """
    Ensure file_index supports project-scoped indexing.

    file_index is disposable cache data, so an older incompatible version can
    be rebuilt safely instead of attempting fragile SQLite constraint changes.
    """
    try:
        if not _table_exists(conn, "file_index"):
            return

        rows = conn.execute(text("PRAGMA table_info(file_index)")).fetchall()
        columns = {row._mapping["name"] for row in rows}
        required_columns = {
            "id",
            "project_id",
            "path",
            "file_type",
            "summary",
            "key_imports",
            "last_modified",
            "token_estimate",
            "line_count",
            "size_bytes",
            "indexed_at",
        }

        if required_columns.issubset(columns):
            return

        conn.execute(text("DROP TABLE file_index"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS file_index (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                summary TEXT,
                key_imports TEXT,
                last_modified DATETIME,
                token_estimate INTEGER DEFAULT 0,
                line_count INTEGER DEFAULT 0,
                size_bytes INTEGER DEFAULT 0,
                indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, path)
            )
        """))
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to ensure file_index shape: {error}"
        )


def _ensure_project_index_fingerprints_shape(conn) -> None:
    """
    Ensure project-level repo-index freshness metadata exists.

    The table stores one checkout identity per project index rebuild. It is
    project-level metadata, not per-file metadata, so file_index shape and
    uniqueness stay untouched.

    #34B introduced this table, so this helper creates it when absent. Unlike
    older shape-repair helpers, it does not rebuild a pre-existing wrong-shaped
    table. Future shape changes should add explicit migration or shape-handling
    logic, as #34C does below for additive snapshot metadata columns.
    """
    try:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS project_index_fingerprints (
                project_id TEXT PRIMARY KEY,
                repo_path_resolved TEXT NOT NULL,
                branch_name TEXT,
                branch_is_detached INTEGER NOT NULL DEFAULT 0,
                detached_head_label TEXT,
                head_sha TEXT NOT NULL,
                dirty_digest TEXT NOT NULL,
                dirty_files_count INTEGER DEFAULT 0,
                index_row_count INTEGER DEFAULT 0,
                captured_at DATETIME NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                snapshot_state TEXT DEFAULT 'current',
                snapshot_reason TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
        """))
        _add_column_if_missing(
            conn,
            "project_index_fingerprints",
            "snapshot_state",
            "ALTER TABLE project_index_fingerprints "
            "ADD COLUMN snapshot_state TEXT DEFAULT 'current'",
        )
        _add_column_if_missing(
            conn,
            "project_index_fingerprints",
            "snapshot_reason",
            "ALTER TABLE project_index_fingerprints ADD COLUMN snapshot_reason TEXT",
        )
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_project_index_fingerprints_updated
            ON project_index_fingerprints(updated_at)
        """))
    except Exception as error:
        raise RuntimeError(
            "database.py: Failed to ensure project_index_fingerprints shape: "
            f"{error}"
        )


def _execute_schema_script(schema_sql: str) -> None:
    """
    Execute a full SQLite schema script.

    Uses the sqlite3 driver's executescript(), which understands SQL comments,
    string literals, and statement boundaries. This replaces naive
    schema_sql.split(";") execution, which broke when a comment or literal
    contained a ';'. Schema statements are idempotent (CREATE ... IF NOT EXISTS).
    """
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        try:
            cursor.executescript(schema_sql)
        finally:
            cursor.close()
        raw_conn.commit()
    finally:
        # Returns the connection to the SQLAlchemy pool (does not hard-close).
        raw_conn.close()


def init_db() -> None:
    """
    Read schema.sql and execute it.
    Creates all tables if they do not exist.
    Safe to call multiple times.
    """
    try:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        _execute_schema_script(schema_sql)
        with engine.connect() as conn:
            _migrate_db(conn)
            conn.commit()
        print("Database initialized successfully.")
        print(f"Database path: {DB_PATH}")
    except Exception as e:
        raise RuntimeError(f"database.py: Failed to initialize database: {e}")


def get_db():
    """
    Dependency for getting a database session.
    Always closes session after use.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
