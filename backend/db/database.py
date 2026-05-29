"""
database.py
SQLAlchemy engine and session setup.
All operations are synchronous.
Database file lives at backend/db/pipewright.db
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from pathlib import Path

DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "pipewright.db"
SCHEMA_PATH = DB_DIR / "schema.sql"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False
)

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


def init_db() -> None:
    """
    Read schema.sql and execute it.
    Creates all tables if they do not exist.
    Safe to call multiple times.
    """
    try:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with engine.connect() as conn:
            for statement in schema_sql.split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(text(statement))
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
