"""
test_sqlite_pragmas.py
Tests for #32D: local SQLite reliability via WAL + busy_timeout.

The session engine (conftest) is bound to a temp SQLite *file* DB, so it
exercises the real production connect hook. WAL is a property of the DB file
and busy_timeout is per-connection, so this is a behavioral assertion only —
no schema or migration change.
"""

import sqlite3
import threading
import time

import pytest
from sqlalchemy import create_engine, event

from backend.db.database import (
    DB_PATH,
    SQLITE_BUSY_TIMEOUT_MS,
    _set_sqlite_pragmas,
    engine,
)

pytestmark = pytest.mark.unit


def test_busy_timeout_is_set_on_engine_connection():
    with engine.connect() as conn:
        value = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert value == SQLITE_BUSY_TIMEOUT_MS


def test_journal_mode_is_wal_on_file_db():
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"


def test_pragmas_are_safe_for_in_memory_sqlite():
    # In-memory SQLite does not support WAL; the real hook must not crash on it
    # (journal_mode stays "memory"), while busy_timeout still applies.
    mem_engine = create_engine("sqlite://")
    event.listen(mem_engine, "connect", _set_sqlite_pragmas)
    try:
        with mem_engine.connect() as conn:
            assert (
                conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
                == SQLITE_BUSY_TIMEOUT_MS
            )
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower()
            # WAL is unsupported for :memory:, so it gracefully stays "memory".
            assert mode in {"memory", "wal"}
    finally:
        event.remove(mem_engine, "connect", _set_sqlite_pragmas)
        mem_engine.dispose()


def test_busy_timeout_lets_contended_writer_wait_instead_of_failing():
    """
    A blocking writer holds the WAL write lock briefly; a second writer that
    came from our engine (and therefore inherits busy_timeout) must wait and
    then succeed, rather than immediately raising 'database is locked'.

    Deterministic: the blocker releases after ~0.3s while busy_timeout is 5s.
    """
    # Ensure WAL is applied to the file before opening plain sqlite3 connections.
    engine.connect().close()

    blocker = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    result = {}
    try:
        blocker.execute("CREATE TABLE IF NOT EXISTS _bt_probe (id INTEGER)")
        blocker.commit()
        # Acquire the single WAL write lock and hold it.
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO _bt_probe (id) VALUES (1)")

        def release_blocker():
            time.sleep(0.3)
            blocker.commit()  # release the write lock

        def contended_write():
            raw = engine.raw_connection()  # busy_timeout=5000 set by the hook
            try:
                cursor = raw.cursor()
                cursor.execute("INSERT INTO _bt_probe (id) VALUES (2)")
                raw.commit()
                result["ok"] = True
            except Exception as error:  # pragma: no cover - failure path
                result["error"] = repr(error)
            finally:
                raw.close()

        releaser = threading.Thread(target=release_blocker)
        writer = threading.Thread(target=contended_write)
        releaser.start()
        writer.start()
        releaser.join()
        writer.join()

        assert result.get("ok") is True, result
    finally:
        try:
            blocker.close()
        except Exception:
            pass
        cleanup = sqlite3.connect(str(DB_PATH))
        cleanup.execute("DROP TABLE IF EXISTS _bt_probe")
        cleanup.commit()
        cleanup.close()
