"""
memory_store.py
Manages Tier 1 hard facts memory layer.
All operations are synchronous SQLite via SQLAlchemy.
AI suggests memory entries. Humans confirm them.
Never auto-saves anything without human confirmation.
"""

import uuid
from datetime import datetime, timedelta
from sqlalchemy import text
from backend.db.database import engine


def load_hard_facts() -> str:
    """
    Load all active non-stale memory facts.
    Returns them as a single newline-joined string
    ready to inject into any model prompt.
    Returns empty string if no facts exist yet.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT content FROM memory_facts
                WHERE is_stale = 0 AND status = 'active'
                ORDER BY created_at ASC
            """))
            facts = [row[0] for row in result.fetchall()]
            return "\n".join(facts) if facts else ""
    except Exception as e:
        print(f"memory_store.py: load_hard_facts failed: {e}")
        return ""


def add_fact(content: str, source: str, added_by: str) -> dict:
    """
    Add a new hard fact to memory.
    Only call this after human has confirmed the entry.
    Raises ValueError if content is empty.
    """
    if not content or not content.strip():
        raise ValueError("memory_store.py: content cannot be empty")

    fact_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    try:
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO memory_facts
                (id, content, source, added_by, created_at, status, is_stale)
                VALUES (:id, :content, :source, :added_by, :now, 'active', 0)
            """), {
                "id": fact_id,
                "content": content.strip(),
                "source": source,
                "added_by": added_by,
                "now": now
            })
            conn.commit()
        return {"id": fact_id, "content": content.strip(), "source": source}
    except Exception as e:
        raise RuntimeError(f"memory_store.py: add_fact failed: {e}")


def flag_stale_memories(days: int = 90) -> int:
    """
    Mark memory entries older than `days` as stale.
    Returns count of entries flagged.
    Call this at the start of every pipeline run.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE memory_facts
                SET is_stale = 1
                WHERE created_at < :cutoff
                AND is_stale = 0
                AND status = 'active'
            """), {"cutoff": cutoff})
            conn.commit()
            return result.rowcount
    except Exception as e:
        print(f"memory_store.py: flag_stale_memories failed: {e}")
        return 0


def list_all_facts() -> list[dict]:
    """
    Return all active facts as list of dicts.
    Used by the UI memory manager screen.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, content, source, added_by,
                       created_at, is_stale, status
                FROM memory_facts
                ORDER BY created_at DESC
            """))
            return [dict(row._mapping) for row in result.fetchall()]
    except Exception as e:
        print(f"memory_store.py: list_all_facts failed: {e}")
        return []
