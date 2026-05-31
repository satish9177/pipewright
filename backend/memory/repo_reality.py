"""
repo_reality.py
Manual, explicit verification of project DB memory against current repo reality.

This is the first behavioral M1.5 slice (#16C). It is invoked only by an explicit
human/API action — never automatically, never on every run, and it never blocks a
pipeline run (run-scope gating is #16D). It does not change prompt injection,
bootstrap output, or any schema.

Core rule (memory-repo-reality-conflicts.md):
  Current repository state > Project State Memory.
  When a deterministic repo DB signal clearly contradicts an active DB memory
  fact, the fact is marked stale (excluded from prompts by the existing
  build_project_memory_block filter). Memory content is never edited, never
  archived, never deleted automatically.

Safety:
  - project_id scoped on every read and write.
  - Unknown or ambiguous repo DB signal never marks memory stale.
  - Evidence is the fingerprint's fixed, human-written excerpt — never raw file
    content, never .env values, never secrets.
"""

from __future__ import annotations

import logging

from backend.memory.memory_store import (
    list_facts,
    mark_fact_stale,
    verify_fact,
)
from backend.repo.repo_fingerprint import build_repo_fingerprint

logger = logging.getLogger(__name__)

# Conservative, deterministic mapping from memory-fact text to a DB engine value.
# Tokens are lowercased substrings; each maps to a repo_fingerprint engine value.
# Order does not matter — multiple distinct matches are treated as ambiguous and
# skipped (never staled). No LLM, no fuzzy matching.
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


def _extract_db_values_from_content(content: str) -> set[str]:
    """
    Return the set of DB engine values clearly mentioned in the fact content.

    Empty set  -> no recognizable DB value (caller skips, never stales).
    One value  -> that engine.
    >1 values  -> ambiguous memory content (caller skips, never stales).
    """
    lowered = (content or "").lower()
    return {
        engine
        for engine, tokens in _DB_VALUE_TOKENS.items()
        if any(token in lowered for token in tokens)
    }


def verify_project_db_memory_against_repo(
    project_id: str,
    repo_path: str | None,
) -> dict:
    """
    Compare active DB-category memory for a project against the repo's
    deterministic DB fingerprint. Returns a structured, project-scoped result.

    Behavior:
      - matching memory + matching repo signal  -> verify_fact (bump
        last_verified_at), kept active.
      - conflicting memory + clear repo signal  -> mark_fact_stale, excluded
        from prompts by the existing builder. Content untouched, not archived.
      - unknown repo signal                     -> no staling.
      - ambiguous repo signal                   -> no staling, warning.
      - no active DB memory                      -> no staling, no error.
      - memory with no recognizable DB value     -> skipped, never staled.
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

    if not repo_path or not str(repo_path).strip():
        result["warnings"].append("project repo_path is missing; nothing verified")
        return result

    fingerprint = build_repo_fingerprint(repo_path)

    # Ambiguous repo DB signal: do not stale anything.
    if fingerprint.db_ambiguous:
        result["ambiguous"] = True
        result["warnings"].append(
            "Repository DB signal is ambiguous (multiple engines detected); "
            "no memory was marked stale."
        )
        return result

    # Unknown repo DB signal: do not stale anything.
    if fingerprint.db is None:
        result["warnings"].append(
            "No deterministic DB signal found in the repository; "
            "no memory was marked stale."
        )
        return result

    repo_db_value = fingerprint.db.value
    result["repo_db_signal"] = repo_db_value

    db_facts = list_facts(project_id, status="active", category="db")
    if not db_facts:
        result["warnings"].append("No active DB memory to verify.")
        return result

    for fact in db_facts:
        fact_id = fact["id"]
        result["checked_count"] += 1
        memory_values = _extract_db_values_from_content(fact.get("content", ""))

        # No recognizable DB value, or content names more than one engine:
        # skip conservatively. Never stale on ambiguous/unknown memory content.
        if len(memory_values) != 1:
            result["skipped_fact_ids"].append(fact_id)
            if len(memory_values) > 1:
                result["warnings"].append(
                    f"Fact {fact_id} mentions multiple DB engines; skipped."
                )
            continue

        memory_value = next(iter(memory_values))

        if memory_value == repo_db_value:
            verify_fact(project_id=project_id, memory_id=fact_id, verified_by="repo-reality")
            result["verified_fact_ids"].append(fact_id)
            continue

        reason = _STALE_REASON_TEMPLATE.format(
            memory_value=memory_value,
            repo_value=repo_db_value,
            evidence_path=fingerprint.db.evidence_path or "repo manifest",
        )
        mark_fact_stale(project_id=project_id, memory_id=fact_id, reason=reason)
        result["staled_fact_ids"].append(fact_id)
        result["evidence"].append({
            "fact_id": fact_id,
            "memory_value": memory_value,
            "repo_value": repo_db_value,
            "evidence_path": fingerprint.db.evidence_path,
            # Fixed human-written string from the fingerprint; never file content.
            "evidence_excerpt": fingerprint.db.evidence_excerpt,
        })

    return result
