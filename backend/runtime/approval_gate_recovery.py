"""
approval_gate_recovery.py
Timeout cleanup for stale pending approval gates.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.core.statuses import ApprovalStatus
from backend.db.database import engine


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for parser in (
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
        lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ),
    ):
        try:
            parsed = parser(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def timeout_stale_approval_gates(hours: int = 2) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    now = datetime.now(timezone.utc).isoformat()
    timed_out_ids: list[str] = []

    try:
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT id, created_at
                FROM approval_gates
                WHERE status = :pending
            """), {"pending": ApprovalStatus.PENDING}).fetchall()

            for row in rows:
                created_at = _parse_timestamp(row._mapping["created_at"])
                if created_at is not None and created_at < cutoff:
                    timed_out_ids.append(row._mapping["id"])

            for gate_id in timed_out_ids:
                conn.execute(text("""
                    UPDATE approval_gates
                    SET status = :timeout,
                        rejection_reason = 'Approval timed out',
                        decided_at = :decided_at
                    WHERE id = :id
                      AND status = :pending
                """), {
                    "timeout": ApprovalStatus.TIMEOUT,
                    "pending": ApprovalStatus.PENDING,
                    "decided_at": now,
                    "id": gate_id,
                })
        return len(timed_out_ids)
    except Exception as error:
        raise RuntimeError(
            f"approval_gate_recovery.py: stale gate cleanup failed: {error}"
        )
