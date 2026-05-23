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
        if not _column_exists(conn, table_name, column_name):
            conn.execute(text(migration_sql))
    except Exception as error:
        raise RuntimeError(
            f"database.py: Failed to add column {table_name}.{column_name}: "
            f"{error}"
        )


def _migrate_db(conn) -> None:
    """
    Apply small SQLite migrations for existing local databases.
    Safe to call multiple times.
    """
    try:
        migrations = [
            (
                "pipeline_runs",
                "project_id",
                "ALTER TABLE pipeline_runs ADD COLUMN project_id TEXT",
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
        ]

        for table_name, column_name, migration_sql in migrations:
            _add_column_if_missing(conn, table_name, column_name, migration_sql)
    except Exception as error:
        raise RuntimeError(f"database.py: Failed to run migrations: {error}")


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
