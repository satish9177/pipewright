"""
injection_analysis.py
Read-only, compute-on-read analysis over persisted memory injection provenance
(M3C2; docs/design/memory-m3-trust-lifecycle.md §16).

Given the immutable injection snapshots recorded by M3C1
(backend/memory/injection_store.py) this module surfaces *advisory candidates*
about the memory facts that were injected into a run — possible near-duplicates
and possible supersession/contradiction pairs — using only the pure, deterministic
M3B helpers (backend/memory/memory_trust.py).

Hard constraints (this module is intentionally pure):
  - No DB access. It operates on already-fetched event dicts handed in by the
    caller; it never opens a session, queries, or writes.
  - No LLM, no embeddings, no vector search.
  - No repo scan, no git, no filesystem, no network.
  - No mutation: it never marks stale/archives/resolves/supersedes, never
    persists analysis, and never changes which memory is injected.
  - Compute-on-read by design: analysis is recomputed from the immutable
    snapshots on every call so evolving heuristics never leave stale stored
    state. Everything returned is labelled candidate/advisory, never fact.

Reality-check analysis (M3F3) is performed ONLY when the caller passes in an
already-computed repo signal map (``repo_signals``). This module never scans the
repo, never calls git, never reads the filesystem: it stays pure, exactly like
the M3B ``check_fact_against_signal`` helper it delegates to. The endpoint owns
the (capped, read-only) repo-signal extraction and hands the result in. With no
signal, no reality warnings are produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from backend.memory.memory_trust import (
    DEFAULT_DUPLICATE_THRESHOLD,
    REALITY_MISMATCH,
    check_fact_against_signal,
    find_duplicate_candidates,
    find_supersession_candidates,
)

DUPLICATE = "duplicate"
SUPERSESSION = "supersession"
REALITY_MISMATCH_CANDIDATE = "reality_mismatch_candidate"


# --- Read-only result models ------------------------------------------------

@dataclass(frozen=True)
class EntryReference:
    """
    Where an injected fact came from, so a human can trace a candidate back to
    the exact provenance event. Carries no content_hash (parity with the rest of
    the memory API); ``content`` is the already-approved fact text the provenance
    endpoint already exposes.
    """
    event_id: str | None
    role: str | None
    chunk_number: int | None
    fact_id: str | None
    content: str


@dataclass(frozen=True)
class DuplicateFinding:
    """Two distinct injected facts that look like the same fact. Advisory only."""
    relation: str  # memory_trust.DUP_EXACT | DUP_NEAR
    similarity: float
    reason: str
    left: EntryReference
    right: EntryReference
    candidate_type: str = DUPLICATE
    advisory_only: bool = True


@dataclass(frozen=True)
class SupersessionFinding:
    """
    Two distinct injected facts that assert different values for the same reality
    dimension. Advisory only; direction is intentionally undecided — a newer fact
    is never automatically correct.
    """
    dimension: str
    left: EntryReference
    right: EntryReference
    left_value: str
    right_value: str
    reason: str
    relation: str  # memory_trust.POSSIBLE_SUPERSESSION
    candidate_type: str = SUPERSESSION
    recency_implies_truth: bool = False
    advisory_only: bool = True


@dataclass(frozen=True)
class RealityWarning:
    """
    One injected fact whose asserted value for a repo dimension disagrees with an
    already-computed repo signal. Advisory only — never a stale/archive/supersede
    decision and never "newer wins". Only ``mismatch`` status is emitted; match /
    unknown / unsupported are intentionally not surfaced as warnings.
    """
    warning_type: str  # REALITY_MISMATCH_CANDIDATE
    dimension: str
    status: str  # always REALITY_MISMATCH for emitted warnings
    fact_id: str | None
    event_id: str | None
    role: str | None
    chunk_number: int | None
    memory_content: str
    memory_value: str | None
    repo_value: str | None
    reason: str
    advisory_only: bool = True


@dataclass(frozen=True)
class InjectionAnalysis:
    """Aggregate advisory analysis for one run's injection provenance."""
    total_events: int = 0
    total_included_entries: int = 0
    distinct_fact_count: int = 0
    duplicate_candidate_count: int = 0
    supersession_candidate_count: int = 0
    duplicate_candidates: tuple[DuplicateFinding, ...] = ()
    supersession_candidates: tuple[SupersessionFinding, ...] = ()
    warnings: tuple[str, ...] = ()
    # M3F3 read-only repo-reality warnings (computed only when a repo signal is
    # passed in). reality_signal_available is True when at least one usable signal
    # was provided, so the UI can distinguish "checked, no mismatch" from "no
    # signal to check against".
    reality_signal_available: bool = False
    reality_warning_count: int = 0
    reality_warnings: tuple[RealityWarning, ...] = ()


# --- Internal accumulation --------------------------------------------------

@dataclass
class _Occurrence:
    event_id: str | None
    role: str | None
    chunk_number: int | None
    fact_id: str | None


@dataclass
class _DistinctFact:
    key: str
    fact_id: str | None
    content: str
    occurrences: list[_Occurrence] = field(default_factory=list)


def _normalize(content: str | None) -> str:
    return " ".join((content or "").lower().split())


def _group_key(entry: dict) -> str:
    """
    Stable identity for a distinct injected fact. The SAME approved fact injected
    into multiple roles/chunks collapses to one distinct fact (so it is never
    flagged as a duplicate of itself); two *different* facts that happen to read
    the same are kept separate by id and can be flagged as duplicates.
    """
    fact_id = entry.get("fact_id")
    if fact_id:
        return f"fact:{fact_id}"
    content_hash = entry.get("content_hash")
    if content_hash:
        return f"hash:{content_hash}"
    return f"norm:{_normalize(entry.get('content'))}"


def _usable_content(entry: dict) -> str | None:
    content = entry.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


# --- Public analysis entry point --------------------------------------------

def analyze_injection_events(
    events: Iterable[dict],
    *,
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    repo_signals: Mapping[str, str] | None = None,
) -> InjectionAnalysis:
    """
    Compute advisory duplicate/supersession candidates over a run's injection
    events. Pure and read-only: ``events`` are the dicts returned by
    injection_store.list_memory_injection_events (or the equivalent shape). No
    DB/LLM/repo/git access; nothing is mutated or persisted.

    ``repo_signals`` (M3F3) is an optional ``{dimension: canonical_repo_value}``
    map the CALLER has already computed (e.g. ``{"db_engine": "postgresql"}``).
    When provided, each distinct injected fact is compared against each signal via
    the pure M3B ``check_fact_against_signal`` helper, and only unambiguous
    ``mismatch`` results are surfaced as advisory ``RealityWarning``s. This module
    still performs NO repo/git/filesystem access — the signal is passed in.

    Returns an empty analysis when there are no events / no usable included
    entries. All findings are advisory candidates, never decisions.
    """
    materialized = list(events or [])
    usable_signals = {
        dimension: value
        for dimension, value in (repo_signals or {}).items()
        if value
    }
    reality_signal_available = bool(usable_signals)
    distinct_order: list[str] = []
    distinct: dict[str, _DistinctFact] = {}
    total_included = 0

    for event in materialized:
        event_id = event.get("id")
        role = event.get("role")
        chunk_number = event.get("chunk_number")
        for entry in event.get("included_entries") or []:
            content = _usable_content(entry)
            if content is None:
                continue
            total_included += 1
            key = _group_key(entry)
            occurrence = _Occurrence(
                event_id=event_id,
                role=role,
                chunk_number=chunk_number,
                fact_id=entry.get("fact_id"),
            )
            existing = distinct.get(key)
            if existing is None:
                distinct_order.append(key)
                distinct[key] = _DistinctFact(
                    key=key,
                    fact_id=entry.get("fact_id"),
                    content=content,
                    occurrences=[occurrence],
                )
            else:
                existing.occurrences.append(occurrence)

    if not distinct_order:
        return InjectionAnalysis(
            total_events=len(materialized),
            reality_signal_available=reality_signal_available,
        )

    items = [{"id": key, "content": distinct[key].content} for key in distinct_order]

    def _ref(key: str) -> EntryReference:
        fact = distinct[key]
        primary = fact.occurrences[0]
        return EntryReference(
            event_id=primary.event_id,
            role=primary.role,
            chunk_number=primary.chunk_number,
            fact_id=fact.fact_id,
            content=fact.content,
        )

    duplicates = tuple(
        DuplicateFinding(
            relation=candidate.relation,
            similarity=candidate.similarity,
            reason=candidate.reason,
            left=_ref(candidate.left_ref),
            right=_ref(candidate.right_ref),
        )
        for candidate in find_duplicate_candidates(items, threshold=threshold)
    )
    supersessions = tuple(
        SupersessionFinding(
            dimension=candidate.dimension,
            left=_ref(candidate.a_ref),
            right=_ref(candidate.b_ref),
            left_value=candidate.a_value,
            right_value=candidate.b_value,
            reason=candidate.reason,
            relation=candidate.relation,
            recency_implies_truth=candidate.recency_implies_truth,
        )
        for candidate in find_supersession_candidates(items)
    )

    # M3F3: advisory repo-reality warnings. Only unambiguous mismatches are
    # surfaced; match/unknown/unsupported are intentionally silent (never scary).
    # The repo signal was computed by the caller — this stays pure.
    reality_warnings: list[RealityWarning] = []
    if usable_signals:
        for key in distinct_order:
            fact = distinct[key]
            primary = fact.occurrences[0]
            for dimension, repo_value in usable_signals.items():
                check = check_fact_against_signal(dimension, fact.content, repo_value)
                if check.status != REALITY_MISMATCH:
                    continue
                reality_warnings.append(RealityWarning(
                    warning_type=REALITY_MISMATCH_CANDIDATE,
                    dimension=dimension,
                    status=check.status,
                    fact_id=fact.fact_id,
                    event_id=primary.event_id,
                    role=primary.role,
                    chunk_number=primary.chunk_number,
                    memory_content=fact.content,
                    memory_value=check.memory_value,
                    repo_value=check.repo_value,
                    reason=(
                        "Repo signal suggests this memory may be outdated: memory "
                        f"indicates '{check.memory_value}' but the repository "
                        f"signal indicates '{check.repo_value}' for {dimension}. "
                        "Advisory candidate only; the system did not change memory."
                    ),
                ))

    return InjectionAnalysis(
        total_events=len(materialized),
        total_included_entries=total_included,
        distinct_fact_count=len(distinct_order),
        duplicate_candidate_count=len(duplicates),
        supersession_candidate_count=len(supersessions),
        duplicate_candidates=duplicates,
        supersession_candidates=supersessions,
        reality_signal_available=reality_signal_available,
        reality_warning_count=len(reality_warnings),
        reality_warnings=tuple(reality_warnings),
    )
