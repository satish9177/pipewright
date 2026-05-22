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
