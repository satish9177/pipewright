"""
memory_trust.py
Pure, deterministic trust-helper foundations for Memory M3 (slice M3B).

These helpers exist to *surface candidates* for a human to review later. They
make no decisions, mutate nothing, and are intentionally NOT wired into any
runtime path in this slice (no planner/coder/triage/reviewer injection, no
routes, no DB writes). See docs/design/memory-m3-trust-lifecycle.md §11 (gap
register) and §13 (slice order).

Hard constraints (enforced by keeping this module stdlib-only):
  - No LLM, no embeddings, no vector search, no external services.
  - No DB session / SQLAlchemy engine / FastAPI / orchestrator / provider import.
  - No filesystem or git access. Reality checks compare a memory fact against an
    ALREADY-COMPUTED repo signal value passed in by the caller; they never scan.
  - No mutation. Every function returns candidate/classification data only.
  - No automatic merge, no auto-resolve, no recency-implies-truth.

What this module deliberately does NOT do:
  - It does not replace the existing exact normalized-hash dedupe
    (memory_store.compute_content_hash); that remains the only thing that
    blocks writes. These helpers only *flag* near-duplicate / contradiction
    candidates for human review.
  - It is not a general semantic conflict engine. Reality and supersession
    checks are category/dimension-specific and deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Mapping

# --- Reality-check result statuses -----------------------------------------

REALITY_MATCH = "match"
REALITY_MISMATCH = "mismatch"
REALITY_UNKNOWN = "unknown"
REALITY_UNSUPPORTED = "unsupported_category"

# --- Candidate relation labels ---------------------------------------------

DUP_EXACT = "exact"
DUP_NEAR = "near"
POSSIBLE_SUPERSESSION = "possible_supersession"

# Default near-duplicate similarity threshold. Conservative on purpose: we
# prefer to miss a near-duplicate than to flag two genuinely different facts.
DEFAULT_DUPLICATE_THRESHOLD = 0.6

# Neutral filler words removed before near-duplicate comparison. Polarity and
# negation words (never/not/no/always/disable/etc.) are INTENTIONALLY absent:
# stripping them would let "Never log secrets" and "Always log secrets" look
# identical, hiding a contradiction and overclaiming a duplicate.
_STOPWORDS = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "being", "been",
    "to", "of", "for", "with", "in", "on", "at", "by", "as",
    "and", "or", "our", "we", "you", "it", "its", "their",
    "via", "using", "use", "used", "uses",
    "run", "runs", "running",
    "project", "projects", "repo", "repository",
})

# Deterministic value vocabulary per reality DIMENSION. A "dimension" is the
# kind of repo signal a memory fact may assert (e.g. which test runner). Each
# canonical value maps to the lowercase match-tokens that evidence it. Tokens
# containing a space are matched as word-bounded phrases against separator-
# normalized text; single-word tokens are matched against the word-token set
# (so "pip" does not match "pipenv"/"pipeline").
#
# This mirrors the conservative one-recognizable-value discipline already used
# by repo_reality.py for the db engine; here it is generalized to more
# dimensions but kept deterministic and token-based (no fuzzy matching).
_DIMENSION_VALUE_TOKENS: dict[str, dict[str, tuple[str, ...]]] = {
    "db_engine": {
        "postgresql": ("postgresql", "postgres", "psycopg", "asyncpg"),
        "mysql": ("mysql", "pymysql", "mysqlclient"),
        "mongodb": ("mongodb", "mongo", "mongoose"),
        "sqlite": ("sqlite", "sqlite3"),
    },
    "backend_framework": {
        "fastapi": ("fastapi",),
        "django": ("django",),
        "flask": ("flask",),
        "express": ("express",),
        "nestjs": ("nestjs", "nest js"),
        "fastify": ("fastify",),
        "spring_boot": ("spring boot", "springboot"),
        "rails": ("rails", "ruby on rails"),
    },
    "frontend_framework": {
        "react": ("react",),
        "vue": ("vue",),
        "angular": ("angular",),
        "svelte": ("svelte",),
        "nextjs": ("nextjs", "next js"),
    },
    "test_runner": {
        "pytest": ("pytest",),
        "unittest": ("unittest",),
        "jest": ("jest",),
        "vitest": ("vitest",),
        "mocha": ("mocha",),
        "junit": ("junit",),
        "rspec": ("rspec",),
        "go_test": ("go test",),
    },
    "migration_tool": {
        "alembic": ("alembic",),
        "prisma": ("prisma",),
        "flyway": ("flyway",),
        "liquibase": ("liquibase",),
        "knex": ("knex",),
        "django_migrations": ("django migrations", "manage py migrate"),
    },
    "package_manager": {
        "npm": ("npm",),
        "yarn": ("yarn",),
        "pnpm": ("pnpm",),
        "pip": ("pip",),
        "poetry": ("poetry",),
        "pipenv": ("pipenv",),
        "cargo": ("cargo",),
        "go_modules": ("go modules", "go mod"),
    },
}

SUPPORTED_REALITY_DIMENSIONS = frozenset(_DIMENSION_VALUE_TOKENS)
# Deterministic dimension order for stable supersession output.
_DIMENSION_ORDER = (
    "db_engine",
    "backend_framework",
    "frontend_framework",
    "test_runner",
    "migration_tool",
    "package_manager",
)


# --- Data models ------------------------------------------------------------

@dataclass(frozen=True)
class DuplicateCandidate:
    """
    A pair of memory items that look like the same fact. Advisory only — never
    a merge instruction. `relation` is DUP_EXACT (normalized text identical) or
    DUP_NEAR (token overlap >= threshold).
    """
    left_ref: str
    right_ref: str
    similarity: float
    relation: str
    shared_tokens: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SupersessionCandidate:
    """
    A pair of memory items that assert different single values for the SAME
    reality dimension (e.g. test_runner pytest vs jest). Advisory only. The
    direction is deliberately undecided: this is a contradiction worth a human
    decision (M3D), not an automatic "newer wins".
    """
    a_ref: str
    b_ref: str
    dimension: str
    a_value: str
    b_value: str
    relation: str
    reason: str
    # Explicit invariant marker so no future caller mistakes this for a decision.
    recency_implies_truth: bool = False
    note: str = (
        "Direction undecided. A newer fact is not automatically correct. "
        "Human resolution required (M3D)."
    )


@dataclass(frozen=True)
class RealityCheckResult:
    """
    Result of comparing one memory fact against an already-computed repo signal
    value for a given dimension. Pure classification; never mutates memory.
    """
    dimension: str
    status: str  # REALITY_MATCH | REALITY_MISMATCH | REALITY_UNKNOWN | REALITY_UNSUPPORTED
    memory_value: str | None
    repo_value: str | None
    reason: str


@dataclass(frozen=True)
class TrustCandidates:
    """Aggregate of advisory candidates for a set of memory items."""
    duplicates: tuple[DuplicateCandidate, ...] = ()
    supersessions: tuple[SupersessionCandidate, ...] = ()


# --- Pure text utilities ----------------------------------------------------

def _simple_normalize(text: str) -> str:
    """Lowercase + whitespace-collapse. Mirrors memory_store normalization."""
    return " ".join((text or "").lower().split())


def _spaced(text: str) -> str:
    """Lowercase with every non-alphanumeric run reduced to a single space."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _word_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _stem(word: str) -> str:
    """Tiny deterministic plural stem: tests->test, runners->runner. Never strips 'ss'."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content_tokens(text: str) -> set[str]:
    """Content-bearing, stopword-filtered, lightly stemmed token set."""
    return {
        _stem(word)
        for word in re.findall(r"[a-z0-9]+", _simple_normalize(text))
        if word not in _STOPWORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _ref(item: Mapping, index: int) -> str:
    """Stable reference for a candidate: the item's id, else 'index:<n>'."""
    value = item.get("id") if isinstance(item, Mapping) else None
    if value is None or str(value).strip() == "":
        return f"index:{index}"
    return str(value)


def _content_of(item: Mapping) -> str | None:
    if not isinstance(item, Mapping):
        return None
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


# --- Near-duplicate candidate detection ------------------------------------

def duplicate_similarity(left_content: str, right_content: str) -> float:
    """
    Deterministic similarity in [0.0, 1.0] for two memory fact texts.

    1.0 for normalized-identical text; otherwise Jaccard overlap of
    stopword-filtered, lightly-stemmed content tokens. Pure; no side effects.
    """
    if _simple_normalize(left_content) == _simple_normalize(right_content):
        return 1.0
    return _jaccard(_content_tokens(left_content), _content_tokens(right_content))


def find_duplicate_candidates(
    items: Iterable[Mapping],
    *,
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
) -> list[DuplicateCandidate]:
    """
    Flag pairs of memory items that look like the same fact. Returns candidate
    info only — it never merges, mutates, or removes anything, and it never
    replaces the exact-hash write-path dedupe.

    items: an iterable of mappings, each with at least a string "content"
    (and optionally an "id"). Items lacking usable content are skipped.
    """
    indexed = list(enumerate(items))
    candidates: list[DuplicateCandidate] = []

    for (left_index, left_item), (right_index, right_item) in combinations(indexed, 2):
        left_content = _content_of(left_item)
        right_content = _content_of(right_item)
        if left_content is None or right_content is None:
            continue

        similarity = duplicate_similarity(left_content, right_content)
        if similarity < threshold:
            continue

        normalized_equal = _simple_normalize(left_content) == _simple_normalize(right_content)
        relation = DUP_EXACT if normalized_equal else DUP_NEAR
        shared = tuple(sorted(_content_tokens(left_content) & _content_tokens(right_content)))
        reason = (
            "Normalized text is identical."
            if normalized_equal
            else (
                f"Token overlap {similarity:.2f} >= {threshold:.2f} "
                f"on shared terms: {', '.join(shared) or '(none)'}."
            )
        )
        candidates.append(DuplicateCandidate(
            left_ref=_ref(left_item, left_index),
            right_ref=_ref(right_item, right_index),
            similarity=round(similarity, 4),
            relation=relation,
            shared_tokens=shared,
            reason=reason,
        ))

    return candidates


# --- Reality dimension value extraction ------------------------------------

def _token_matches(token: str, word_set: set[str], spaced_text: str) -> bool:
    if " " in token:
        return f" {token} " in f" {spaced_text} "
    return token in word_set


def extract_dimension_values(dimension: str, content: str) -> set[str]:
    """
    Return the set of canonical values for `dimension` clearly mentioned in
    `content`. Empty set means no recognizable value; >1 means ambiguous. Pure.

    Returns an empty set for an unsupported dimension (callers should check
    SUPPORTED_REALITY_DIMENSIONS when the distinction matters).
    """
    vocab = _DIMENSION_VALUE_TOKENS.get(dimension)
    if not vocab:
        return set()
    word_set = _word_tokens(content)
    spaced_text = _spaced(content)
    return {
        value
        for value, tokens in vocab.items()
        if any(_token_matches(token, word_set, spaced_text) for token in tokens)
    }


def _single_dimension_value(dimension: str, content: str) -> str | None:
    values = extract_dimension_values(dimension, content)
    return next(iter(values)) if len(values) == 1 else None


def _canonical_repo_value(dimension: str, repo_value: str | None) -> str | None:
    """
    Map a caller-provided repo signal value to a canonical dimension value.

    Accepts either an already-canonical value (e.g. "postgresql") or a short
    token that the dimension recognizes (e.g. "postgres"). Returns None when the
    value is empty or not recognizable for the dimension (insufficient signal).
    """
    if repo_value is None:
        return None
    text = repo_value.strip().lower()
    if not text:
        return None
    vocab = _DIMENSION_VALUE_TOKENS.get(dimension) or {}
    if text in vocab:
        return text
    matched = extract_dimension_values(dimension, text)
    return next(iter(matched)) if len(matched) == 1 else None


# --- Non-DB reality-check helper (pure comparison, never scans) ------------

def check_fact_against_signal(
    dimension: str,
    content: str,
    repo_value: str | None,
) -> RealityCheckResult:
    """
    Compare a single memory fact's asserted value for `dimension` against an
    ALREADY-COMPUTED repo signal value. Pure classification only — never reads
    the repo, never calls git, never marks stale, never archives, never writes.

    Outcomes:
      - REALITY_UNSUPPORTED: dimension not in SUPPORTED_REALITY_DIMENSIONS.
      - REALITY_UNKNOWN: no/ambiguous value in the fact, or no recognizable
        repo signal. (Conservative: never escalates to mismatch.)
      - REALITY_MATCH: fact value equals the repo signal value.
      - REALITY_MISMATCH: fact value differs from the repo signal value.
    """
    if dimension not in SUPPORTED_REALITY_DIMENSIONS:
        return RealityCheckResult(
            dimension=dimension,
            status=REALITY_UNSUPPORTED,
            memory_value=None,
            repo_value=repo_value,
            reason=f"Dimension '{dimension}' is not a supported reality check.",
        )

    memory_value = _single_dimension_value(dimension, content)
    canonical_repo = _canonical_repo_value(dimension, repo_value)

    if memory_value is None:
        return RealityCheckResult(
            dimension=dimension,
            status=REALITY_UNKNOWN,
            memory_value=None,
            repo_value=canonical_repo,
            reason="Memory fact has no single recognizable value for this dimension.",
        )
    if canonical_repo is None:
        return RealityCheckResult(
            dimension=dimension,
            status=REALITY_UNKNOWN,
            memory_value=memory_value,
            repo_value=None,
            reason="No recognizable repo signal value for this dimension.",
        )
    if memory_value == canonical_repo:
        return RealityCheckResult(
            dimension=dimension,
            status=REALITY_MATCH,
            memory_value=memory_value,
            repo_value=canonical_repo,
            reason=f"Memory value '{memory_value}' matches repo signal.",
        )
    return RealityCheckResult(
        dimension=dimension,
        status=REALITY_MISMATCH,
        memory_value=memory_value,
        repo_value=canonical_repo,
        reason=(
            f"Memory value '{memory_value}' differs from repo signal "
            f"'{canonical_repo}'."
        ),
    )


# --- Supersession / contradiction candidate detection ----------------------

def find_supersession_candidates(
    items: Iterable[Mapping],
) -> list[SupersessionCandidate]:
    """
    Flag pairs of memory items that assert different single values for the same
    reality dimension (a contradiction worth a human decision). Returns
    candidate info only: it never picks a winner, never uses recency, never
    archives or mutates anything.
    """
    indexed = list(enumerate(items))
    candidates: list[SupersessionCandidate] = []

    for (a_index, a_item), (b_index, b_item) in combinations(indexed, 2):
        a_content = _content_of(a_item)
        b_content = _content_of(b_item)
        if a_content is None or b_content is None:
            continue
        for dimension in _DIMENSION_ORDER:
            a_value = _single_dimension_value(dimension, a_content)
            b_value = _single_dimension_value(dimension, b_content)
            if a_value is None or b_value is None or a_value == b_value:
                continue
            candidates.append(SupersessionCandidate(
                a_ref=_ref(a_item, a_index),
                b_ref=_ref(b_item, b_index),
                dimension=dimension,
                a_value=a_value,
                b_value=b_value,
                relation=POSSIBLE_SUPERSESSION,
                reason=(
                    f"Both facts assert a {dimension} value but disagree: "
                    f"'{a_value}' vs '{b_value}'."
                ),
            ))

    return candidates


# --- Aggregate convenience --------------------------------------------------

def find_trust_candidates(
    items: Iterable[Mapping],
    *,
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
) -> TrustCandidates:
    """
    Run both candidate detectors once over `items`. Pure; advisory only. Does
    not mutate input or memory and is not wired into any runtime path.
    """
    materialized = list(items)
    return TrustCandidates(
        duplicates=tuple(find_duplicate_candidates(materialized, threshold=threshold)),
        supersessions=tuple(find_supersession_candidates(materialized)),
    )
