"""
memory_store.py
Project-scoped hard facts memory layer.

Memory is advisory and human-approved. AI must not auto-save long-term memory.
All operations are synchronous SQLite via SQLAlchemy.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.db.database import engine

logger = logging.getLogger(__name__)

CONTENT_MIN_LENGTH = 4
CONTENT_MAX_LENGTH = 400
DEFAULT_PRIORITY = 100
MEMORY_SAFETY_ERROR = (
    "Memory entries cannot contain secrets, credentials, emails, phone "
    "numbers, or payment card numbers."
)
PRE_M1_ARCHIVE_REASON = "pre-M1 unscoped memory archived for project-safety"

ALLOWED_CATEGORIES = {
    "stack",
    "structure",
    "test",
    "db",
    "style",
    "security",
    "architecture",
    "deploy",
    "forbidden_paths",
    "reviewer_pref",
    "other",
}
ALLOWED_SCOPES = {"global", "backend", "frontend", "tests", "infra"}
ALLOWED_STATUSES = {"active", "stale", "archived", "historical"}

CATEGORY_ORDER = {
    "security": 0,
    "forbidden_paths": 1,
    "stack": 2,
    "db": 3,
    "test": 4,
    "structure": 5,
    "architecture": 6,
    "style": 7,
    "deploy": 8,
    "reviewer_pref": 9,
    "other": 10,
}

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bghp_[0-9A-Za-z]{20,}\b"),
    re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b"),
    re.compile(r"\bxox[abpr s]-[0-9A-Za-z-]{10,}\b".replace(" ", "")),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]
LONG_HEX_PATTERN = re.compile(r"\b[a-fA-F0-9]{40,}\b")
GIT_HASH_CONTEXT_PATTERN = re.compile(
    r"\b(?:commit|sha|git hash|git commit|commit hash)\s+"
    r"[a-fA-F0-9]{40,}\b",
    re.IGNORECASE,
)

PROMPT_INJECTION_MARKERS = [
    "SYSTEM:",
    "ASSISTANT:",
    "USER:",
    "<|",
    "```system",
    "=== PROJECT MEMORY",
    "ignore previous instructions",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_content(content: str) -> str:
    return " ".join(content.strip().lower().split())


def compute_content_hash(content: str) -> str:
    normalized = _normalize_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _luhn_valid(digits: str) -> bool:
    total = 0
    reverse_digits = digits[::-1]
    for index, char in enumerate(reverse_digits):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _contains_payment_card(content: str) -> bool:
    for candidate in re.findall(r"(?:\d[ -]?){13,19}", content):
        digits = re.sub(r"\D", "", candidate)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return True
    return False


def _contains_phone_number(content: str) -> bool:
    for candidate in re.findall(r"(?:\+?\d[\d\s().-]{8,}\d)", content):
        digits = re.sub(r"\D", "", candidate)
        has_phone_context = candidate.strip().startswith("+") or bool(
            re.search(r"[\s().-]", candidate)
        )
        if has_phone_context and 10 <= len(digits) <= 15:
            return True
    return False


def _contains_unexplained_long_hex(content: str) -> bool:
    for match in LONG_HEX_PATTERN.finditer(content):
        start = max(0, match.start() - 24)
        context = content[start:match.end()]
        if GIT_HASH_CONTEXT_PATTERN.search(context):
            continue
        return True
    return False


def validate_memory_content(content: str) -> str:
    if content is None:
        raise ValueError("memory_store.py: content is required")

    value = content.strip()
    if len(value) < CONTENT_MIN_LENGTH:
        raise ValueError(
            f"memory_store.py: content must be at least {CONTENT_MIN_LENGTH} characters"
        )
    if len(value) > CONTENT_MAX_LENGTH:
        raise ValueError(
            f"memory_store.py: content cannot exceed {CONTENT_MAX_LENGTH} characters"
        )

    lowered = value.lower()
    if any(marker.lower() in lowered for marker in PROMPT_INJECTION_MARKERS):
        raise ValueError("memory_store.py: memory content contains unsafe instructions")

    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise ValueError(MEMORY_SAFETY_ERROR)
    if _contains_unexplained_long_hex(value):
        raise ValueError(MEMORY_SAFETY_ERROR)
    if _contains_phone_number(value):
        raise ValueError(MEMORY_SAFETY_ERROR)
    if _contains_payment_card(value):
        raise ValueError(MEMORY_SAFETY_ERROR)

    return value


def _validate_project_id(project_id: str | None) -> str:
    if not project_id or not str(project_id).strip():
        raise ValueError("memory_store.py: project_id is required")
    return str(project_id).strip()


def _validate_category(category: str | None) -> str:
    value = (category or "other").strip()
    if value not in ALLOWED_CATEGORIES:
        raise ValueError(f"memory_store.py: invalid memory category: {value}")
    return value


def _validate_scope(scope: str | None) -> str:
    value = (scope or "global").strip()
    if value not in ALLOWED_SCOPES:
        raise ValueError(f"memory_store.py: invalid memory scope: {value}")
    return value


def _validate_priority(priority: int | None) -> int:
    value = DEFAULT_PRIORITY if priority is None else int(priority)
    if value < 0 or value > 1000:
        raise ValueError("memory_store.py: priority must be between 0 and 1000")
    return value


def _estimate_tokens(text_value: str) -> int:
    return max(1, (len(text_value) + 3) // 4)


def _archive_unscoped_pre_m1_memory() -> None:
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE memory_facts
            SET status = 'archived',
                is_stale = 1,
                archived_reason = COALESCE(archived_reason, :reason),
                updated_at = :now
            WHERE project_id IS NULL
              AND status = 'active'
        """), {"reason": PRE_M1_ARCHIVE_REASON, "now": _utc_now()})
        conn.commit()


def add_fact(
    project_id: str,
    content: str,
    category: str = "other",
    scope: str = "global",
    priority: int = DEFAULT_PRIORITY,
    source: str = "manual",
    added_by: str = "user",
    approved_by: str | None = None,
) -> dict:
    """
    Add a project-scoped memory fact after human confirmation.
    Duplicate active facts are blocked per project by content hash.
    """
    project_id = _validate_project_id(project_id)
    content = validate_memory_content(content)
    category = _validate_category(category)
    scope = _validate_scope(scope)
    priority = _validate_priority(priority)
    content_hash = compute_content_hash(content)
    fact_id = str(uuid.uuid4())
    now = _utc_now()

    try:
        _archive_unscoped_pre_m1_memory()
        with engine.connect() as conn:
            existing = conn.execute(text("""
                SELECT id FROM memory_facts
                WHERE project_id = :project_id
                  AND content_hash = :content_hash
                  AND status = 'active'
                LIMIT 1
            """), {
                "project_id": project_id,
                "content_hash": content_hash,
            }).fetchone()
            if existing:
                raise ValueError(
                    "memory_store.py: active duplicate memory fact already exists"
                )

            conn.execute(text("""
                INSERT INTO memory_facts
                (id, project_id, content, category, scope, priority, status,
                 is_stale, source, added_by, approved_by, approved_at,
                 content_hash, created_at, updated_at)
                VALUES
                (:id, :project_id, :content, :category, :scope, :priority,
                 'active', 0, :source, :added_by, :approved_by, :approved_at,
                 :content_hash, :now, :now)
            """), {
                "id": fact_id,
                "project_id": project_id,
                "content": content,
                "category": category,
                "scope": scope,
                "priority": priority,
                "source": source,
                "added_by": added_by,
                "approved_by": approved_by,
                "approved_at": now if approved_by else None,
                "content_hash": content_hash,
                "now": now,
            })
            conn.commit()
        return {
            "id": fact_id,
            "project_id": project_id,
            "content": content,
            "category": category,
            "scope": scope,
            "priority": priority,
            "status": "active",
            "source": source,
            "added_by": added_by,
            "approved_by": approved_by,
            "content_hash": content_hash,
        }
    except ValueError:
        raise
    except IntegrityError as error:
        raise ValueError(
            "memory_store.py: active duplicate memory fact already exists"
        ) from error
    except Exception as error:
        raise RuntimeError(f"memory_store.py: add_fact failed: {error}") from error


def list_facts(
    project_id: str,
    status: str | None = None,
    category: str | None = None,
    scope: str | None = None,
) -> list[dict]:
    project_id = _validate_project_id(project_id)
    filters = ["project_id = :project_id"]
    params = {"project_id": project_id}

    if status is not None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"memory_store.py: invalid memory status: {status}")
        filters.append("status = :status")
        params["status"] = status
    if category is not None:
        params["category"] = _validate_category(category)
        filters.append("category = :category")
    if scope is not None:
        params["scope"] = _validate_scope(scope)
        filters.append("scope = :scope")

    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT id, project_id, content, category, scope, priority,
                       status, is_stale, source, added_by, approved_by,
                       approved_at, last_verified_at, archived_reason,
                       content_hash, created_at, updated_at
                FROM memory_facts
                WHERE {" AND ".join(filters)}
                ORDER BY created_at DESC
            """), params)
            return [dict(row._mapping) for row in result.fetchall()]
    except Exception as error:
        raise RuntimeError(f"memory_store.py: list_facts failed: {error}") from error


def load_hard_facts(
    project_id: str | None = None,
    role: str | None = None,
    token_budget: int | None = None,
) -> str:
    """
    Load active project-scoped facts for prompt context.
    Missing project_id deliberately returns no memory to prevent global leaks.
    """
    if not project_id:
        try:
            _archive_unscoped_pre_m1_memory()
        except Exception as error:
            logger.warning(
                "memory_store.py: failed to archive unscoped memory: %s",
                error,
            )
        logger.warning("memory_store.py: load_hard_facts called without project_id")
        return ""

    try:
        project_id = _validate_project_id(project_id)
        _archive_unscoped_pre_m1_memory()
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT content, category, priority, created_at
                FROM memory_facts
                WHERE project_id = :project_id
                  AND is_stale = 0
                  AND status = 'active'
                ORDER BY created_at ASC
            """), {"project_id": project_id})
            rows = [dict(row._mapping) for row in result.fetchall()]

        rows.sort(key=lambda row: (
            CATEGORY_ORDER.get(row["category"] or "other", CATEGORY_ORDER["other"]),
            int(row["priority"] or DEFAULT_PRIORITY),
            row["created_at"] or "",
        ))

        selected: list[str] = []
        used_tokens = 0
        for row in rows:
            content = row["content"]
            content_tokens = _estimate_tokens(content)
            separator_tokens = 1 if selected else 0
            if token_budget is not None:
                if used_tokens + separator_tokens + content_tokens > token_budget:
                    continue
            selected.append(content)
            used_tokens += separator_tokens + content_tokens

        return "\n".join(selected)
    except Exception as error:
        logger.warning("memory_store.py: load_hard_facts failed: %s", error)
        return ""


def archive_fact(
    project_id: str,
    memory_id: str,
    reason: str,
    archived_by: str | None = None,
) -> dict:
    project_id = _validate_project_id(project_id)
    reason_value = (reason or "").strip()
    if len(reason_value) < CONTENT_MIN_LENGTH:
        raise ValueError("memory_store.py: archived reason is required")

    try:
        now = _utc_now()
        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE memory_facts
                SET status = 'archived',
                    is_stale = 1,
                    archived_reason = :reason,
                    updated_at = :now
                WHERE id = :memory_id
                  AND project_id = :project_id
            """), {
                "memory_id": memory_id,
                "project_id": project_id,
                "reason": reason_value,
                "now": now,
            })
            conn.commit()
        if result.rowcount == 0:
            raise ValueError("memory_store.py: memory fact not found")
        return {"id": memory_id, "project_id": project_id, "status": "archived"}
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"memory_store.py: archive_fact failed: {error}") from error


def verify_fact(
    project_id: str,
    memory_id: str,
    verified_by: str | None = None,
) -> dict:
    project_id = _validate_project_id(project_id)
    try:
        now = _utc_now()
        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE memory_facts
                SET last_verified_at = :now,
                    updated_at = :now
                WHERE id = :memory_id
                  AND project_id = :project_id
            """), {
                "memory_id": memory_id,
                "project_id": project_id,
                "now": now,
            })
            conn.commit()
        if result.rowcount == 0:
            raise ValueError("memory_store.py: memory fact not found")
        return {
            "id": memory_id,
            "project_id": project_id,
            "last_verified_at": now,
        }
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"memory_store.py: verify_fact failed: {error}") from error


def update_fact(
    project_id: str,
    memory_id: str,
    content: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    priority: int | None = None,
) -> dict:
    project_id = _validate_project_id(project_id)
    fields: list[str] = []
    params = {
        "memory_id": memory_id,
        "project_id": project_id,
        "now": _utc_now(),
    }

    if content is not None:
        content = validate_memory_content(content)
        content_hash = compute_content_hash(content)
        try:
            with engine.connect() as conn:
                existing = conn.execute(text("""
                    SELECT id FROM memory_facts
                    WHERE project_id = :project_id
                      AND content_hash = :content_hash
                      AND status = 'active'
                      AND id != :memory_id
                    LIMIT 1
                """), {
                    "project_id": project_id,
                    "content_hash": content_hash,
                    "memory_id": memory_id,
                }).fetchone()
            if existing:
                raise ValueError(
                    "memory_store.py: active duplicate memory fact already exists"
                )
        except ValueError:
            raise
        fields.extend(["content = :content", "content_hash = :content_hash"])
        params["content"] = content
        params["content_hash"] = content_hash
    if category is not None:
        fields.append("category = :category")
        params["category"] = _validate_category(category)
    if scope is not None:
        fields.append("scope = :scope")
        params["scope"] = _validate_scope(scope)
    if priority is not None:
        fields.append("priority = :priority")
        params["priority"] = _validate_priority(priority)

    if not fields:
        raise ValueError("memory_store.py: no updates provided")

    fields.append("updated_at = :now")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                UPDATE memory_facts
                SET {", ".join(fields)}
                WHERE id = :memory_id
                  AND project_id = :project_id
            """), params)
            conn.commit()
        if result.rowcount == 0:
            raise ValueError("memory_store.py: memory fact not found")
        facts = list_facts(project_id)
        return next(fact for fact in facts if fact["id"] == memory_id)
    except ValueError:
        raise
    except IntegrityError as error:
        raise ValueError(
            "memory_store.py: active duplicate memory fact already exists"
        ) from error
    except Exception as error:
        raise RuntimeError(f"memory_store.py: update_fact failed: {error}") from error


def flag_stale_memories(days: int = 90, project_id: str | None = None) -> int:
    try:
        if project_id is None:
            logger.warning(
                "memory_store.py: flag_stale_memories called without project_id"
            )
            return 0

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        filters = ["created_at < :cutoff", "is_stale = 0", "status = 'active'"]
        params = {
            "cutoff": cutoff,
            "now": _utc_now(),
            "project_id": _validate_project_id(project_id),
        }
        filters.append("project_id = :project_id")
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                UPDATE memory_facts
                SET is_stale = 1,
                    status = 'stale',
                    updated_at = :now
                WHERE {" AND ".join(filters)}
            """), params)
            conn.commit()
            return result.rowcount
    except Exception as error:
        logger.warning("memory_store.py: flag_stale_memories failed: %s", error)
        return 0


def list_all_facts(project_id: str | None = None) -> list[dict]:
    """
    Compatibility helper. Without project_id, only returns scoped rows and
    never treats unscoped legacy rows as active memory.
    """
    try:
        _archive_unscoped_pre_m1_memory()
        with engine.connect() as conn:
            if project_id:
                result = conn.execute(text("""
                    SELECT id, project_id, content, category, scope, priority,
                           source, added_by, created_at, is_stale, status
                    FROM memory_facts
                    WHERE project_id = :project_id
                    ORDER BY created_at DESC
                """), {"project_id": _validate_project_id(project_id)})
            else:
                result = conn.execute(text("""
                    SELECT id, project_id, content, category, scope, priority,
                           source, added_by, created_at, is_stale, status
                    FROM memory_facts
                    WHERE project_id IS NOT NULL
                    ORDER BY created_at DESC
                """))
            return [dict(row._mapping) for row in result.fetchall()]
    except Exception as error:
        logger.warning("memory_store.py: list_all_facts failed: %s", error)
        return []
