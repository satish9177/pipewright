"""
clarification_selection.py
Deterministic parser for clarification replies (PR #17L).

Given the candidate list that was previously shown (e.g. three indexed README
files) and a free-form user reply, map the reply to **exactly one** candidate or
report it as unrecognized. There is no LLM, no fuzzy matching, no "closest
candidate", and no module-level state: the candidate list is always passed in
explicitly, so "1" has no meaning outside a clarification context.

Accepted replies (candidates are 1-based in the order shown):
  - index forms:        ``1``, ``#1``, ``1.``, ``option 1``, ``use 1``,
                        ``yes 1``, ``select 1``
  - exact path forms:   ``README.md``, ``use README.md``, ``docs/adr/README.md``
                        (must match one candidate after separator normalization)
  - affirmation only:   ``yes`` / ``confirm`` -> the ``recommended_path``, and
                        only when a recommendation exists.

Everything else (``0``/``4``, ``2 or 3``, ``both``, ``the main one``, empty,
garbage, a path not in candidates, or ``yes`` with no recommendation) is
``UNRECOGNIZED``. An unrecognized reply must never create a run; the caller
re-clarifies.

Path normalization: a small *local* normalizer (separator/edge cleanup only,
case-sensitive) is used deliberately instead of importing
``file_alias_grounding._normalize_path_token`` — that module transitively imports
``repo_indexer`` and the DB engine, which a pure parser must not pull in.
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence

from pydantic import BaseModel


class SelectionStatus(str, Enum):
    SELECTED = "selected"
    UNRECOGNIZED = "unrecognized"


class SelectionResult(BaseModel):
    status: SelectionStatus
    selected_path: str | None = None
    reason: str | None = None

    @classmethod
    def selected(cls, path: str, reason: str) -> "SelectionResult":
        return cls(
            status=SelectionStatus.SELECTED,
            selected_path=path,
            reason=reason,
        )

    @classmethod
    def unrecognized(cls, reason: str) -> "SelectionResult":
        return cls(status=SelectionStatus.UNRECOGNIZED, reason=reason)


# Leading words that may precede a real selection and carry no meaning of their
# own. Matched case-insensitively, token by token. "#" is handled per-token so
# "#1" still resolves to index 1.
_LEADING_WORDS = {
    "yes",
    "use",
    "pick",
    "choose",
    "select",
    "option",
    "number",
    "confirm",
    "the",
}
_AFFIRMATION_WORDS = {"yes", "confirm"}

# Harmless punctuation stripped from the edges of the single remaining token so
# "1.", "#1", "(1)" resolve. Stripped from both ends only; internal characters
# (e.g. the dot in "README.md") are untouched.
_EDGE_PUNCT = "#.,;:!?()[]{}'\"`"


def _normalize_path_for_match(value: str) -> str:
    """
    Case-sensitive separator/edge cleanup for path comparison. Backslashes ->
    forward slashes; leading ``./`` and surrounding ``/`` removed. Casing is
    preserved so a wrong-case reply does not match (the caller re-clarifies).
    """
    token = value.strip().replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    return token.strip("/")


def parse_clarification_selection(
    reply: str,
    candidates: Sequence[str],
    recommended_path: str | None,
) -> SelectionResult:
    """
    Map ``reply`` to exactly one of ``candidates`` (or recommend), else
    UNRECOGNIZED. Pure and deterministic; does not mutate its arguments.
    """
    candidate_list = list(candidates)
    if not isinstance(reply, str):
        return SelectionResult.unrecognized("reply is not a string")

    tokens = reply.split()
    if not tokens:
        return SelectionResult.unrecognized("empty reply")

    # Strip leading no-op words, remembering whether an affirmation appeared.
    affirmation_seen = False
    index = 0
    while index < len(tokens):
        base = tokens[index].lower()
        if base in _LEADING_WORDS:
            if base in _AFFIRMATION_WORDS:
                affirmation_seen = True
            index += 1
            continue
        if base == "#":
            index += 1
            continue
        break
    remaining = tokens[index:]

    # Affirmation only ("yes" / "confirm") -> the recommendation, if any.
    if not remaining:
        if affirmation_seen and recommended_path is not None:
            return SelectionResult.selected(
                recommended_path,
                reason="affirmation:recommended",
            )
        return SelectionResult.unrecognized(
            "no selection after leading words"
        )

    # A clean selection is a single remaining token (an index or a path). A
    # multi-token remainder ("2 or 3", "the main one") is deliberately ambiguous.
    if len(remaining) > 1:
        return SelectionResult.unrecognized("multiple selection tokens")

    token = remaining[0].strip(_EDGE_PUNCT)
    if not token:
        return SelectionResult.unrecognized("empty selection token")

    # Index form.
    if token.isdigit():
        number = int(token)
        if 1 <= number <= len(candidate_list):
            return SelectionResult.selected(
                candidate_list[number - 1],
                reason="index",
            )
        return SelectionResult.unrecognized(f"index out of range: {number}")

    # Exact-path form: match exactly one candidate after separator normalization.
    normalized_token = _normalize_path_for_match(token)
    matches = [
        candidate
        for candidate in candidate_list
        if _normalize_path_for_match(candidate) == normalized_token
    ]
    if len(matches) == 1:
        return SelectionResult.selected(matches[0], reason="path")

    return SelectionResult.unrecognized("reply did not match any candidate")
