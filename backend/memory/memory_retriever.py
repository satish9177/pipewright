"""
Memory candidate retrieval seam.

PR-B keeps a single deterministic rung-0 retriever. It loads the same canonical
memory_facts rows prompt_builder loaded before this seam and does not read FTS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

from backend.db.database import engine

if TYPE_CHECKING:
    from backend.memory.prompt_builder import RequestContext


@dataclass(frozen=True)
class RetrievedCandidates:
    in_policy_rows: list[dict]
    out_of_policy_rows: list[dict]


class MemoryRetriever(Protocol):
    def retrieve_candidates(
        self,
        project_id: str,
        categories: set[str],
        request_context: RequestContext | None,
    ) -> RetrievedCandidates:
        ...


class DeterministicMemoryRetriever:
    """Rung-0: current active memory_facts candidate load and partition."""

    def retrieve_candidates(
        self,
        project_id: str,
        categories: set[str],
        request_context: RequestContext | None = None,
    ) -> RetrievedCandidates:
        del request_context

        if not project_id or not str(project_id).strip():
            return RetrievedCandidates(in_policy_rows=[], out_of_policy_rows=[])

        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, content, content_hash, category, scope, priority, status,
                       created_at
                FROM memory_facts
                WHERE project_id = :project_id
                  AND is_stale = 0
                  AND status = 'active'
            """), {"project_id": str(project_id).strip()})
            rows = [dict(row._mapping) for row in result.fetchall()]

        in_policy: list[dict] = []
        out_of_policy: list[dict] = []
        for row in rows:
            if (row.get("category") or "other") in categories:
                in_policy.append(row)
            else:
                out_of_policy.append(row)
        return RetrievedCandidates(
            in_policy_rows=in_policy,
            out_of_policy_rows=out_of_policy,
        )


_DEFAULT_MEMORY_RETRIEVER = DeterministicMemoryRetriever()


def default_memory_retriever() -> MemoryRetriever:
    """Return the deterministic rung-0 retriever. PR-B has no alternate rung."""
    return _DEFAULT_MEMORY_RETRIEVER
