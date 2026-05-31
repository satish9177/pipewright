"""
repo_reality.py
DB memory vs. repo reality: a pure read-only evaluator plus the explicit manual
verification action.

Memory M1.5:
  - evaluate_db_memory_conflicts(...)         -> pure, read-only conflict detection
    (#16D-3). Builds the repo fingerprint, compares it to project DB memory, and
    returns a ConflictReport. It NEVER mutates memory (no verify, no stale, no DB
    writes). Used by the non-blocking warning surface and, later, the gate (#16D-4).
  - verify_project_db_memory_against_repo(...) -> the manual action (#16C). It calls
    the evaluator and is the ONLY place that mutates memory (verify matches, stale
    conflicts), via the explicit /memory/verify-repo endpoint.

Core rule (memory-repo-reality-conflicts.md): current repository state > project
memory. Memory content is never edited, never archived, never deleted automatically.

Safety:
  - project_id scoped on every read and write.
  - Unknown or ambiguous repo DB signal never marks memory stale.
  - Evidence is the fingerprint's fixed, human-written excerpt — never raw file
    content, never .env values, never secrets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.memory.memory_store import (
    list_facts,
    mark_fact_stale,
    verify_fact,
)
from backend.repo.repo_fingerprint import build_repo_fingerprint

logger = logging.getLogger(__name__)

# Conservative, deterministic mapping from memory-fact text to a DB engine value.
# Tokens are lowercased substrings; each maps to a repo_fingerprint engine value.
# Multiple distinct matches are treated as ambiguous and skipped (never staled).
# No LLM, no fuzzy matching.
_DB_VALUE_TOKENS = {
    "postgresql": ("postgres",),
    "mysql": ("mysql",),
    "mongodb": ("mongo",),
    "sqlite": ("sqlite",),
}

_STALE_REASON_TEMPLATE = (
    "Repo reality conflict: memory indicates {memory_value} but the repository "
    "fingerprint indicates {repo_value} ({evidence_path})."
)

# Statuses the evaluator considers by default. Active facts are injected; stale
# facts still represent a wrong belief, so both are worth surfacing as conflicts.
_DEFAULT_EVAL_STATUSES = ("active", "stale")


@dataclass(frozen=True)
class ConflictEntry:
    """One DB memory fact that disagrees with the repo signal. Read-only detail."""
    fact_id: str
    memory_value: str
    repo_value: str
    evidence_path: str | None
    evidence_excerpt: str  # fingerprint's fixed string; never raw file content
    status: str            # the memory fact's status at evaluation time


@dataclass(frozen=True)
class ConflictReport:
    """
    Read-only result of comparing project DB memory against the repo fingerprint.
    Produced without mutating memory.
    """
    repo_db_signal: str | None
    ambiguous: bool
    repo_path_missing: bool
    checked_count: int
    conflicts: tuple[ConflictEntry, ...]   # memory value differs from the repo value
    matches: tuple[str, ...]               # fact_ids whose value equals the repo value
    skipped: tuple[str, ...]               # fact_ids with 0 or >1 recognizable engines
    multi_engine: tuple[str, ...]          # subset of skipped naming more than one engine
    warnings: tuple[str, ...]              # neutral, read-only notices

    @property
    def evidence(self) -> tuple[ConflictEntry, ...]:
        return self.conflicts


def _extract_db_values_from_content(content: str) -> set[str]:
    """
    Return the set of DB engine values clearly mentioned in the fact content.

    Empty set  -> no recognizable DB value (skip; never stale).
    One value  -> that engine.
    >1 values  -> ambiguous memory content (skip; never stale).
    """
    lowered = (content or "").lower()
    return {
        engine
        for engine, tokens in _DB_VALUE_TOKENS.items()
        if any(token in lowered for token in tokens)
    }


def evaluate_db_memory_conflicts(
    project_id: str,
    repo_path: str | None,
    statuses: tuple[str, ...] = _DEFAULT_EVAL_STATUSES,
) -> ConflictReport:
    """
    Pure, read-only comparison of a project's DB memory against the repo's DB
    fingerprint. Never mutates memory (no verify_fact, no mark_fact_stale, no DB
    writes). project_id scoped.

    Returns a ConflictReport. Repo path missing / unknown DB signal / ambiguous DB
    signal short-circuit with the corresponding flag set and no facts compared.
    """
    if not repo_path or not str(repo_path).strip():
        return ConflictReport(
            repo_db_signal=None, ambiguous=False, repo_path_missing=True,
            checked_count=0, conflicts=(), matches=(), skipped=(),
            multi_engine=(),
            warnings=("Repository path is unavailable; DB conflict evaluation skipped.",),
        )

    fingerprint = build_repo_fingerprint(repo_path)

    if fingerprint.db_ambiguous:
        return ConflictReport(
            repo_db_signal=None, ambiguous=True, repo_path_missing=False,
            checked_count=0, conflicts=(), matches=(), skipped=(), multi_engine=(),
            warnings=("Repository DB signal is ambiguous (multiple engines detected).",),
        )

    if fingerprint.db is None:
        return ConflictReport(
            repo_db_signal=None, ambiguous=False, repo_path_missing=False,
            checked_count=0, conflicts=(), matches=(), skipped=(), multi_engine=(),
            warnings=("No deterministic DB signal found in the repository.",),
        )

    repo_db_value = fingerprint.db.value

    facts = [
        fact for fact in list_facts(project_id, category="db")
        if (fact.get("status") or "") in statuses
    ]
    if not facts:
        return ConflictReport(
            repo_db_signal=repo_db_value, ambiguous=False, repo_path_missing=False,
            checked_count=0, conflicts=(), matches=(), skipped=(), multi_engine=(),
            warnings=("No DB memory facts to evaluate.",),
        )

    conflicts: list[ConflictEntry] = []
    matches: list[str] = []
    skipped: list[str] = []
    multi_engine: list[str] = []

    for fact in facts:
        fact_id = fact["id"]
        memory_values = _extract_db_values_from_content(fact.get("content", ""))
        if len(memory_values) != 1:
            skipped.append(fact_id)
            if len(memory_values) > 1:
                multi_engine.append(fact_id)
            continue
        memory_value = next(iter(memory_values))
        if memory_value == repo_db_value:
            matches.append(fact_id)
            continue
        conflicts.append(ConflictEntry(
            fact_id=fact_id,
            memory_value=memory_value,
            repo_value=repo_db_value,
            evidence_path=fingerprint.db.evidence_path,
            evidence_excerpt=fingerprint.db.evidence_excerpt,
            status=(fact.get("status") or ""),
        ))

    return ConflictReport(
        repo_db_signal=repo_db_value,
        ambiguous=False,
        repo_path_missing=False,
        checked_count=len(facts),
        conflicts=tuple(conflicts),
        matches=tuple(matches),
        skipped=tuple(skipped),
        multi_engine=tuple(multi_engine),
        warnings=(),
    )


def verify_project_db_memory_against_repo(
    project_id: str,
    repo_path: str | None,
) -> dict:
    """
    Manual action: compare active DB memory against the repo fingerprint and apply
    mutations — verify matching facts (bump last_verified_at), mark conflicting
    facts stale. This is the ONLY place in this module that mutates memory.

    Considers active facts only (the original #16C scope). Returns the same
    structured, project-scoped result shape as before.
    """
    result: dict = {
        "project_id": project_id,
        "repo_db_signal": None,
        "ambiguous": False,
        "checked_count": 0,
        "verified_fact_ids": [],
        "staled_fact_ids": [],
        "skipped_fact_ids": [],
        "warnings": [],
        "evidence": [],
    }

    report = evaluate_db_memory_conflicts(project_id, repo_path, statuses=("active",))
    result["repo_db_signal"] = report.repo_db_signal
    result["ambiguous"] = report.ambiguous
    result["checked_count"] = report.checked_count
    result["skipped_fact_ids"] = list(report.skipped)

    if report.repo_path_missing:
        result["warnings"].append("project repo_path is missing; nothing verified")
        return result
    if report.ambiguous:
        result["warnings"].append(
            "Repository DB signal is ambiguous (multiple engines detected); "
            "no memory was marked stale."
        )
        return result
    if report.repo_db_signal is None:
        result["warnings"].append(
            "No deterministic DB signal found in the repository; "
            "no memory was marked stale."
        )
        return result
    if report.checked_count == 0:
        result["warnings"].append("No active DB memory to verify.")
        return result

    for fact_id in report.matches:
        verify_fact(project_id=project_id, memory_id=fact_id, verified_by="repo-reality")
        result["verified_fact_ids"].append(fact_id)

    for entry in report.conflicts:
        reason = _STALE_REASON_TEMPLATE.format(
            memory_value=entry.memory_value,
            repo_value=entry.repo_value,
            evidence_path=entry.evidence_path or "repo manifest",
        )
        mark_fact_stale(project_id=project_id, memory_id=entry.fact_id, reason=reason)
        result["staled_fact_ids"].append(entry.fact_id)
        result["evidence"].append({
            "fact_id": entry.fact_id,
            "memory_value": entry.memory_value,
            "repo_value": entry.repo_value,
            "evidence_path": entry.evidence_path,
            "evidence_excerpt": entry.evidence_excerpt,
        })

    for fact_id in report.multi_engine:
        result["warnings"].append(
            f"Fact {fact_id} mentions multiple DB engines; skipped."
        )

    return result
