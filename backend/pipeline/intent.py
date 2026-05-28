"""
Deterministic request intent classification.

This module is intentionally conservative: uncertain requests default to
plan_only, not implementation.
"""

from typing import Literal

Intent = Literal["report_only", "plan_only", "implementation"]

REPORT_ONLY = "report_only"
PLAN_ONLY = "plan_only"
IMPLEMENTATION = "implementation"


_REPORT_PHRASES = [
    "find bugs",
    "check for bugs",
    "look for bugs",
    "list issues",
    "review",
    "audit",
    "analyze",
    "explain",
    "what does",
    "how does",
    "describe",
    "summarize",
    "inspect",
    "is there any issue",
    "any issue",
    "any issues",
    "any bug",
    "any bugs",
    "any problem",
    "any problems",
    "issues in code",
    "bugs in code",
    "problems in code",
    "what is wrong",
    "what's wrong",
    "just explain",
    "just describe",
    "just review",
    "just analyze",
]

_STRONG_PLAN_PHRASES = [
    "give me a plan",
    "plan",
    "chunk plan",
    "break into chunks",
    "how would you",
    "what would it take",
    "suggest an approach",
]

_SOFT_PLAN_PHRASES = [
    "design",
    "architect",
    "propose",
    "outline",
    "break down",
]

_IMPLEMENTATION_PHRASES = [
    "add",
    "create",
    "implement",
    "build",
    "fix",
    "change",
    "modify",
    "update",
    "refactor",
    "migrate",
    "remove",
    "delete",
    "replace",
    "upgrade",
    "integrate",
    "write",
]

_READ_ONLY_SAFETY_PHRASES = [
    "don't change code",
    "do not change code",
    "dont change code",
    "don't edit",
    "do not edit",
    "dont edit",
    "don't modify",
    "do not modify",
    "dont modify",
    "don't write",
    "do not write",
    "dont write",
    "don't add",
    "do not add",
    "dont add",
    "don't fix",
    "do not fix",
    "dont fix",
    "don't implement",
    "do not implement",
    "dont implement",
    "only explain",
    "only review",
    "only audit",
    "no code changes",
    "change any single line",
    "read only",
    "read-only",
    "without changing",
    "without modifying",
]


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("’", "'").split())


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def classify_intent(feature_description: str) -> Intent:
    text = _normalize(feature_description or "")
    if not text:
        return PLAN_ONLY

    has_report = _contains_any(text, _REPORT_PHRASES)
    has_strong_plan = _contains_any(text, _STRONG_PLAN_PHRASES)
    has_soft_plan = _contains_any(text, _SOFT_PLAN_PHRASES)
    has_plan = has_strong_plan or has_soft_plan
    has_implementation = _contains_any(text, _IMPLEMENTATION_PHRASES)
    has_read_only_safety = _contains_any(text, _READ_ONLY_SAFETY_PHRASES)

    if has_read_only_safety:
        if has_plan:
            return PLAN_ONLY
        return REPORT_ONLY

    if has_report and not has_implementation:
        return REPORT_ONLY

    if has_strong_plan:
        return PLAN_ONLY

    if has_implementation:
        return IMPLEMENTATION

    if has_soft_plan:
        return PLAN_ONLY

    return PLAN_ONLY
