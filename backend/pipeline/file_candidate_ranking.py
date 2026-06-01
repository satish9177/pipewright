"""
file_candidate_ranking.py
Pure deterministic ranking for ambiguous file candidates (PR #17I).

The helper never reads the DB, filesystem, index, or LLM. It only reorders the
candidate paths it is given and may attach a conservative recommendation for
clarification UX. It never selects or drops a path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class Recommendation(str, Enum):
    NONE = "none"
    WEAK = "weak"
    STRONG = "strong"


@dataclass(frozen=True)
class RankedCandidateResult:
    ordered: tuple[str, ...]
    recommended: str | None
    recommendation: Recommendation
    reason_tokens: tuple[str, ...]


@dataclass(frozen=True)
class _CandidateScore:
    path: str
    score: int
    matched_tokens: tuple[str, ...]
    excluded_tokens: tuple[str, ...]
    exact_match: bool
    root_level: bool
    root_cue: bool
    depth_only: bool


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CLAUSE_BOUNDARY_RE = re.compile(r"[,.;]")
_STOPWORDS = {
    "the",
    "a",
    "an",
    "in",
    "to",
    "of",
    "and",
    "or",
    "file",
    "files",
    "please",
    "add",
    "edit",
    "into",
    "on",
    "at",
    "with",
}
_ROOT_CUES = {
    "main",
    "root",
    "top",
    "top-level",
    "toplevel",
    "project",
    "primary",
    "base",
}
_NEGATION_CUES = (
    "other than",
    "rather than",
    "instead of",
    "excluding",
    "without",
    "except",
    "not",
    "no",
)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall((value or "").lower()))


def _candidate_tokens(path: str) -> frozenset[str]:
    return frozenset(_tokens(path))


def _depth(path: str) -> int:
    return max(0, len([part for part in path.replace("\\", "/").split("/") if part]) - 1)


def _has_root_cue(request_text: str) -> bool:
    text = (request_text or "").lower()
    request_tokens = set(_tokens(text))
    return bool(request_tokens & (_ROOT_CUES - {"top-level"})) or "top-level" in text


def _exact_path_mentioned(request_text: str, candidate: str) -> bool:
    request = (request_text or "").lower().replace("\\", "/")
    path = candidate.lower().replace("\\", "/")
    return path in request


def _negation_clauses(request_text: str) -> tuple[str, ...]:
    clauses: list[str] = []
    for clause in _CLAUSE_BOUNDARY_RE.split((request_text or "").lower()):
        for cue in _NEGATION_CUES:
            index = clause.find(cue)
            if index == -1:
                continue
            after = clause[index + len(cue):].strip()
            if after:
                clauses.append(after)
            break
    return tuple(clauses)


def _negation_tokens_and_exact_paths(
    request_text: str,
    candidates: Sequence[str],
) -> tuple[frozenset[str], frozenset[str]]:
    clauses = _negation_clauses(request_text)
    negated_tokens: set[str] = set()
    exact_paths: set[str] = set()

    for clause in clauses:
        negated_tokens.update(token for token in _tokens(clause) if token not in _STOPWORDS)
        normalized_clause = clause.replace("\\", "/")
        for candidate in candidates:
            if candidate.lower().replace("\\", "/") in normalized_clause:
                exact_paths.add(candidate)

    # Exact paths may contain "." characters, which are token clause boundaries.
    # Scan comma/semicolon-delimited spans too so "not README.md" still marks the
    # exact candidate as negated.
    for span in re.split(r"[,;]", (request_text or "").lower()):
        for cue in _NEGATION_CUES:
            index = span.find(cue)
            if index == -1:
                continue
            after = span[index + len(cue):].replace("\\", "/")
            for candidate in candidates:
                if candidate.lower().replace("\\", "/") in after:
                    exact_paths.add(candidate)
            break

    return frozenset(negated_tokens), frozenset(exact_paths)


def _common_candidate_tokens(candidates: Sequence[str]) -> frozenset[str]:
    if not candidates:
        return frozenset()
    common = set(_candidate_tokens(candidates[0]))
    for candidate in candidates[1:]:
        common &= set(_candidate_tokens(candidate))
    return frozenset(common)


def _score_candidates(
    request_text: str,
    candidates: tuple[str, ...],
) -> tuple[_CandidateScore, ...]:
    common_tokens = _common_candidate_tokens(candidates)
    negation_tokens, exact_negated_paths = _negation_tokens_and_exact_paths(
        request_text,
        candidates,
    )
    request_tokens = {
        token for token in _tokens(request_text)
        if token not in _STOPWORDS
        and token not in negation_tokens
        and token not in common_tokens
    }
    root_cue = _has_root_cue(request_text)
    max_depth = max((_depth(candidate) for candidate in candidates), default=0)
    scored: list[_CandidateScore] = []

    for candidate in candidates:
        path_tokens = _candidate_tokens(candidate)
        unique_path_tokens = path_tokens - common_tokens
        score = 0
        exact_match = _exact_path_mentioned(request_text, candidate)
        if exact_match:
            score += 100

        excluded = set(unique_path_tokens & negation_tokens)
        if candidate in exact_negated_paths:
            excluded.update(unique_path_tokens or path_tokens)
        if excluded:
            score -= 100

        matched = set(unique_path_tokens & request_tokens)
        score += 20 * len(matched)

        depth = _depth(candidate)
        root_level = depth == 0
        depth_bonus = max_depth - depth
        if root_cue:
            score += 5 * depth_bonus
        else:
            score += depth_bonus

        scored.append(_CandidateScore(
            path=candidate,
            score=score,
            matched_tokens=tuple(sorted(matched)),
            excluded_tokens=tuple(sorted(excluded)),
            exact_match=exact_match,
            root_level=root_level,
            root_cue=root_cue,
            depth_only=(
                not exact_match
                and not matched
                and not excluded
                and depth_bonus > 0
            ),
        ))

    return tuple(scored)


def _reason_tokens(top: _CandidateScore, scored: Sequence[_CandidateScore]) -> tuple[str, ...]:
    reasons: list[str] = []
    if top.exact_match:
        reasons.append("exact-path")
    if top.root_level and top.root_cue:
        reasons.append("root-level")
    reasons.extend(f"matched:{token}" for token in top.matched_tokens)

    excluded_tokens = sorted({
        token
        for candidate in scored
        if candidate.path != top.path
        for token in candidate.excluded_tokens
    })
    reasons.extend(f"excluded:{token}" for token in excluded_tokens)
    return tuple(reasons)


def rank_ambiguous_file_candidates(
    request_text: str,
    candidates: Sequence[str],
) -> RankedCandidateResult:
    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        return RankedCandidateResult(
            ordered=(),
            recommended=None,
            recommendation=Recommendation.NONE,
            reason_tokens=(),
        )
    if len(candidate_tuple) == 1:
        return RankedCandidateResult(
            ordered=candidate_tuple,
            recommended=candidate_tuple[0],
            recommendation=Recommendation.STRONG,
            reason_tokens=("single-candidate",),
        )

    scored = _score_candidates(request_text, candidate_tuple)
    if all(candidate.excluded_tokens for candidate in scored):
        return RankedCandidateResult(
            ordered=tuple(sorted(candidate_tuple)),
            recommended=None,
            recommendation=Recommendation.NONE,
            reason_tokens=(),
        )

    ordered_scores = tuple(sorted(scored, key=lambda item: (-item.score, item.path)))
    ordered = tuple(item.path for item in ordered_scores)
    top = ordered_scores[0]
    second = ordered_scores[1]
    if top.score == second.score:
        return RankedCandidateResult(
            ordered=ordered,
            recommended=None,
            recommendation=Recommendation.NONE,
            reason_tokens=(),
        )

    reasons = _reason_tokens(top, scored)
    margin = top.score - second.score
    has_discriminating_signal = bool(
        top.exact_match
        or top.matched_tokens
        or (top.root_cue and top.root_level)
        or any(candidate.excluded_tokens for candidate in scored if candidate.path != top.path)
    )
    if has_discriminating_signal and margin >= 10:
        recommendation = Recommendation.STRONG
        recommended = top.path
    elif top.depth_only:
        recommendation = Recommendation.WEAK
        recommended = top.path
    else:
        recommendation = Recommendation.NONE
        recommended = None
        reasons = ()

    return RankedCandidateResult(
        ordered=ordered,
        recommended=recommended,
        recommendation=recommendation,
        reason_tokens=reasons,
    )
