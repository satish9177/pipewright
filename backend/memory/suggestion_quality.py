"""
suggestion_quality.py
Deterministic quality scorer for unstructured handoff memory suggestions.

This module is intentionally pure and string-only: no LLM calls, no database
access, no filesystem access, and no repo/log reading. The objective-junk
predicate is the only flooring decision. Numeric scores are ranking metadata
for handoff candidates only; low-scoring non-junk suggestions still reach the
pending queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class SuggestionQuality(str, Enum):
    # Name begins with "Suggestion", so tell pytest not to try to collect it as
    # a test class.
    __test__ = False

    JUNK = "junk"
    WEAK = "weak"
    OK = "ok"
    STRONG = "strong"


@dataclass(frozen=True)
class SuggestionQualityResult:
    score: int
    quality: SuggestionQuality
    reasons: list[str]
    floored: bool


_REFERENCE_TOKEN = r"(?:\#?\d+|[a-f0-9][a-f0-9-]{3,35})"
_PURE_REFERENCE_RE = re.compile(
    rf"""
    ^(?:
        (?:(?:this|that|the)\s+)?
        (?:run|chunk|attempt)
        (?:\s+{_REFERENCE_TOKEN})?
        (?:\s*(?:(?:,|/|and)\s*)?
            (?:(?:this|that|the)\s+)?
            (?:run|chunk|attempt)
            (?:\s+{_REFERENCE_TOKEN})?
        )*
        |
        (?:run|chunk|attempt)\s+{_REFERENCE_TOKEN}\s+
        (?:completed|complete|failed|rejected|succeeded|success)
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_LOW_INFO_DENYLIST = {
    "uses python",
    "project uses python",
    "backend uses python",
    "frontend uses python",
    "uses fastapi",
    "project uses fastapi",
    "backend uses fastapi",
    "uses react",
    "frontend uses react",
    "uses typescript",
    "project uses typescript",
    "has tests",
    "project has tests",
    "repo has tests",
    "repository has tests",
}

_CONSTRAINT_RE = re.compile(
    r"\b(?:never|must|must not|do not|don't|cannot|should not|required to|"
    r"always)\b",
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"\b(?:lives?|is located|are located|belongs?|resides?)\s+in\b",
    re.IGNORECASE,
)
_COMMAND_RE = re.compile(
    r"\b(?:run tests with|test command|run .*pytest|pytest\b|npm\s+test|"
    r"npm\s+run\s+test|pnpm\s+test|yarn\s+test|go\s+test|cargo\s+test)\b",
    re.IGNORECASE,
)
_SAFETY_OR_CONVENTION_RE = re.compile(
    r"(?:\b(?:secrets?|tokens?|approval|scope_guard|scope guard|advisory|"
    r"source code wins|prefer|convention|safety|forbidden|do not commit)\b|"
    r"(?:^|\s)\.env\b)",
    re.IGNORECASE,
)
_PROJECT_SPECIFIC_RE = re.compile(
    r"\b(?:backend|frontend|tests?|pipeline|memory|chunk|planner|coder|"
    r"pytest|fastapi|scope_guard|patch_applier)\b|"
    r"(?:^|\s)[\w.-]+/[\w./-]+|"
    r"\b[\w.-]+\.(?:py|ts|tsx|js|jsx|sql|toml|json|yaml|yml|md)\b",
    re.IGNORECASE,
)


def _normalize(content: str | None) -> str:
    value = "" if content is None else str(content)
    collapsed = " ".join(value.strip().split()).lower()
    return collapsed.strip(" .;:!?\"'")


def is_objective_junk(content: str) -> bool:
    """
    Return True only for deterministic junk that should not enter the queue.

    This is intentionally narrower than "low quality": a weak but non-junk
    suggestion must remain visible for human review.
    """
    normalized = _normalize(content)
    if not normalized:
        return True
    if _PURE_REFERENCE_RE.match(normalized):
        return True
    return normalized in _LOW_INFO_DENYLIST


def _quality_for_score(score: int) -> SuggestionQuality:
    if score >= 70:
        return SuggestionQuality.STRONG
    if score >= 40:
        return SuggestionQuality.OK
    return SuggestionQuality.WEAK


def score_suggestion(
    content: str,
    category: str = "other",
) -> SuggestionQualityResult:
    """
    Score an unstructured memory suggestion for handoff ranking.

    ``floored`` is derived only from :func:`is_objective_junk`; callers must not
    suppress candidates because of a low numeric score or WEAK quality.
    """
    normalized = _normalize(content)
    reasons: list[str] = []
    if is_objective_junk(content):
        return SuggestionQualityResult(
            score=0,
            quality=SuggestionQuality.JUNK,
            reasons=["objective_junk"],
            floored=True,
        )

    score = 15
    if _CONSTRAINT_RE.search(normalized):
        score += 30
        reasons.append("constraint")
    if _LOCATION_RE.search(normalized):
        score += 25
        reasons.append("location")
    if _COMMAND_RE.search(normalized):
        score += 40
        reasons.append("command")
    if _SAFETY_OR_CONVENTION_RE.search(normalized):
        score += 25
        reasons.append("durable_convention")
    if _PROJECT_SPECIFIC_RE.search(normalized):
        score += 15
        reasons.append("project_specific")

    category_value = (category or "other").strip().lower()
    if category_value and category_value != "other":
        score += 10
        reasons.append("category_specific")

    if not reasons:
        reasons.append("low_signal_non_junk")

    score = max(1, min(score, 100))
    return SuggestionQualityResult(
        score=score,
        quality=_quality_for_score(score),
        reasons=reasons,
        floored=False,
    )
