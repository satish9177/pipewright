"""
Memory candidate retrieval seam.

PR-B keeps a single deterministic rung-0 retriever. It loads the same canonical
memory_facts rows prompt_builder loaded before this seam and does not read FTS.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

from backend.db.database import engine
from backend.memory.memory_fts import _rank_memory_fts_for_project
from backend.pipeline import policy as pipeline_policy

if TYPE_CHECKING:
    from backend.memory.prompt_builder import RequestContext

_FTS_OPERATOR_TOKENS = frozenset({"and", "or", "not", "near"})
_FTS_COLUMN_FILTER_RE = re.compile(r"\b[a-zA-Z_][A-Za-z0-9_]*\s*:\s*\S+")
_REQUEST_TOKEN_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class RetrievedCandidates:
    in_policy_rows: list[dict]
    out_of_policy_rows: list[dict]
    relevance_scores: dict[str, float] | None = None


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


class FTSMemoryRetriever:
    """
    Rung-1 FTS retriever.

    FTS is only additive relevance metadata. The full candidate set always comes
    from the deterministic retriever, so FTS can neither add nor drop facts.
    """

    def __init__(self, base_retriever: MemoryRetriever | None = None) -> None:
        self._base_retriever = base_retriever or DeterministicMemoryRetriever()

    def retrieve_candidates(
        self,
        project_id: str,
        categories: set[str],
        request_context: RequestContext | None = None,
    ) -> RetrievedCandidates:
        candidates = self._base_retriever.retrieve_candidates(
            project_id,
            categories,
            request_context=request_context,
        )
        query_tokens = _fts_query_tokens(request_context)
        if not query_tokens:
            return candidates

        canonical_rows = candidates.in_policy_rows + candidates.out_of_policy_rows
        canonical_by_id = {
            row.get("id"): row
            for row in canonical_rows
            if row.get("id")
        }
        if not canonical_by_id:
            return candidates

        try:
            ranked_hits = _rank_memory_fts_for_project(
                str(project_id).strip(),
                query_tokens,
            )
        except Exception:
            ranked_hits = []
        if not ranked_hits:
            return candidates

        relevance_scores: dict[str, float] = {}
        for hit in ranked_hits:
            fact_id = hit.get("fact_id")
            canonical = canonical_by_id.get(fact_id)
            if canonical is None:
                continue
            if hit.get("content_hash") != canonical.get("content_hash"):
                continue
            score = float(hit.get("score") or 0.0)
            if score > 0:
                relevance_scores[fact_id] = score

        if not relevance_scores:
            return candidates
        return RetrievedCandidates(
            in_policy_rows=candidates.in_policy_rows,
            out_of_policy_rows=candidates.out_of_policy_rows,
            relevance_scores=relevance_scores,
        )


def _fts_query_tokens(request_context: RequestContext | None) -> tuple[str, ...]:
    if request_context is None:
        return ()

    raw_text = " ".join(
        str(part)
        for part in (
            request_context.title,
            request_context.description,
            request_context.steer_text,
            " ".join(str(path) for path in request_context.files_expected),
        )
        if part
    )
    if not raw_text:
        return ()
    raw_text = _FTS_COLUMN_FILTER_RE.sub(" ", raw_text)

    tokens: list[str] = []
    seen: set[str] = set()
    for match in _REQUEST_TOKEN_RE.finditer(raw_text.lower()):
        token = match.group(0)
        if len(token) < 2 or token in _FTS_OPERATOR_TOKENS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= pipeline_policy.MEMORY_FTS_QUERY_TOKEN_CAP:
            break
    return tuple(tokens)


_DEFAULT_MEMORY_RETRIEVER = DeterministicMemoryRetriever()
_DEFAULT_FTS_MEMORY_RETRIEVER = FTSMemoryRetriever(_DEFAULT_MEMORY_RETRIEVER)


def default_memory_retriever() -> MemoryRetriever:
    """Return the active memory retriever. FTS rung-1 is default-off."""
    if pipeline_policy.MEMORY_FTS_RETRIEVAL_ENABLED:
        return _DEFAULT_FTS_MEMORY_RETRIEVER
    return _DEFAULT_MEMORY_RETRIEVER
