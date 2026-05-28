"""
Deterministic request intent classification with an LLM fallback.

Layers (in order):
  1. Read-only safety blockers (absolute; never call LLM)
  2. Deterministic report keywords (when no implementation verb is present)
  3. Strong plan markers (explicit "plan" / "give me a plan" / ...)
  4. Implementation verbs
  5. Soft plan markers (description words like "design", "architect", ...)
  6. LLM fallback (only when none of the above matched and no read-only blocker)
  7. Safe default: plan_only

The sync ``classify_intent`` only runs layers 1-5 + 7. The async
``classify_intent_async`` adds layer 6.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Literal

from backend.llm import Role, complete_for_role
from backend.llm.base import LLMRequest, Message
from backend.llm.errors import ProviderConfigurationError, UnsupportedProviderError

Intent = Literal["report_only", "plan_only", "implementation"]

REPORT_ONLY: Intent = "report_only"
PLAN_ONLY: Intent = "plan_only"
IMPLEMENTATION: Intent = "implementation"

logger = logging.getLogger(__name__)

LLM_MAX_DESCRIPTION_CHARS = 300
LLM_MAX_OUTPUT_TOKENS = 50
LLM_TIMEOUT_SECONDS = 10
LLM_IMPLEMENTATION_MIN_CONFIDENCE = 0.8

_ALLOWED_INTENTS = {REPORT_ONLY, PLAN_ONLY, IMPLEMENTATION}


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
    # User-story / Jira phrasing usually represents implementation work.
    "as a user, i want",
    "as an admin, i want",
    "as a customer, i want",
    "as a developer, i want",
    "user story:",
    "acceptance criteria:",
    # Improve / cleanup phrasing (multi-word forms).
    "make better",
    "clean up",
    "cleanup",
]

# Implementation patterns that need word boundaries to avoid matching
# nominal forms like "improvements" (a noun the user wants reported on).
_IMPLEMENTATION_REGEXES = [
    re.compile(r"\bimprove\b"),
    re.compile(r"\boptimize\b"),
    # General "as a <role>, i want" pattern (single-word role).
    re.compile(r"\bas an? \w+, i want\b"),
]

_READ_ONLY_SAFETY_PHRASES = [
    "don't change code",
    "do not change code",
    "dont change code",
    "don't change anything",
    "do not change anything",
    "dont change anything",
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
    "don't improve",
    "do not improve",
    "dont improve",
    "don't clean",
    "do not clean",
    "dont clean",
    "don't optimize",
    "do not optimize",
    "dont optimize",
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

_LLM_SYSTEM_PROMPT = (
    "You classify a single software-engineering request into exactly one of "
    "three intents:\n"
    "  - report_only: the user wants information, explanation, audit, or "
    "diagnosis only; no code changes.\n"
    "  - plan_only: the user wants a plan, design, or approach; no code "
    "changes.\n"
    "  - implementation: the user wants code to be written or modified.\n"
    "Respond with strict JSON only, matching this schema:\n"
    '  {"intent": "report_only|plan_only|implementation",\n'
    '   "confidence": 0.0-1.0,\n'
    '   "reason": "one short sentence"}\n'
    "Do not include any text outside the JSON object."
)

_LLM_USER_PROMPT_TEMPLATE = (
    "Classify this request:\n---\n{text}\n---\n"
    "Return only the JSON object."
)


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("’", "'").split())


def _contains_any(text: str, phrases: list[str]) -> tuple[bool, list[str]]:
    matched = [phrase for phrase in phrases if phrase in text]
    return bool(matched), matched


def _matches_any_regex(
    text: str, patterns: list[re.Pattern[str]]
) -> tuple[bool, list[str]]:
    matched = [p.search(text).group(0) for p in patterns if p.search(text)]
    return bool(matched), matched


def _deterministic_classify(
    text: str,
) -> tuple[Intent | None, str, dict[str, list[str]]]:
    """
    Run deterministic layers on already-normalized text.

    Returns (intent_or_none, source_label, matched_patterns).
    A None intent means no deterministic layer matched; caller decides
    whether to fall through to LLM or to a safe default.
    """
    if not text:
        return None, "ambiguous", {}

    has_safety, safety_matches = _contains_any(text, _READ_ONLY_SAFETY_PHRASES)
    has_report, report_matches = _contains_any(text, _REPORT_PHRASES)
    has_strong_plan, strong_plan_matches = _contains_any(text, _STRONG_PLAN_PHRASES)
    has_soft_plan, soft_plan_matches = _contains_any(text, _SOFT_PLAN_PHRASES)
    has_impl_phrase, impl_phrase_matches = _contains_any(
        text, _IMPLEMENTATION_PHRASES
    )
    has_impl_regex, impl_regex_matches = _matches_any_regex(
        text, _IMPLEMENTATION_REGEXES
    )
    has_implementation = has_impl_phrase or has_impl_regex
    impl_matches = impl_phrase_matches + impl_regex_matches

    matched: dict[str, list[str]] = {}
    if has_safety:
        matched["read_only_safety"] = safety_matches
    if has_report:
        matched["report"] = report_matches
    if has_strong_plan:
        matched["strong_plan"] = strong_plan_matches
    if has_soft_plan:
        matched["soft_plan"] = soft_plan_matches
    if has_implementation:
        matched["implementation"] = impl_matches

    if has_safety:
        if has_strong_plan or has_soft_plan:
            return PLAN_ONLY, "deterministic_blocker", matched
        return REPORT_ONLY, "deterministic_blocker", matched

    if has_report and not has_implementation:
        return REPORT_ONLY, "deterministic_keyword", matched

    if has_strong_plan:
        return PLAN_ONLY, "deterministic_keyword", matched

    if has_implementation:
        return IMPLEMENTATION, "deterministic_verb", matched

    if has_soft_plan:
        return PLAN_ONLY, "deterministic_keyword", matched

    return None, "ambiguous", matched


def _log_intent_decision(
    *,
    intent: Intent,
    source: str,
    matched: dict[str, list[str]],
    description_truncated: bool,
    duration_ms: int,
    confidence: float | None = None,
    reason: str | None = None,
) -> None:
    logger.info(
        "[INTENT] intent=%s source=%s confidence=%s matched=%s "
        "description_truncated=%s duration_ms=%s reason=%s",
        intent,
        source,
        f"{confidence:.2f}" if confidence is not None else "none",
        sorted(matched.keys()),
        description_truncated,
        duration_ms,
        reason or "none",
    )


def classify_intent(feature_description: str) -> Intent:
    """
    Synchronous, deterministic-only classifier. Never calls the LLM.

    Returns ``plan_only`` for ambiguous requests (safe default).
    """
    start = time.monotonic()
    text = _normalize(feature_description or "")
    intent, source, matched = _deterministic_classify(text)
    if intent is None:
        intent = PLAN_ONLY
        source = "default"
    duration_ms = int((time.monotonic() - start) * 1000)
    _log_intent_decision(
        intent=intent,
        source=source,
        matched=matched,
        description_truncated=False,
        duration_ms=duration_ms,
    )
    return intent


def _parse_llm_intent_payload(
    text: str,
) -> tuple[tuple[Intent, float, str | None] | None, str | None]:
    """
    Parse an LLM payload into (intent, confidence, reason).

    Returns ``(parsed, error_code)``. On success ``error_code`` is ``None``.
    On failure ``parsed`` is ``None`` and ``error_code`` is one of
    ``"invalid_json"`` or ``"invalid_schema"``.
    """
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "invalid_schema"

    raw_intent = parsed.get("intent")
    if not isinstance(raw_intent, str) or raw_intent not in _ALLOWED_INTENTS:
        return None, "invalid_schema"

    raw_confidence = parsed.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        return None, "invalid_schema"
    confidence = float(raw_confidence)
    if not (0.0 <= confidence <= 1.0):
        return None, "invalid_schema"

    reason = parsed.get("reason")
    reason_text = reason if isinstance(reason, str) else None

    return (raw_intent, confidence, reason_text), None  # type: ignore[return-value]


async def _llm_fallback_classify(
    feature_description: str,
) -> tuple[Intent, str, float | None, str | None]:
    """
    Call the LLM for an ambiguous request and return a safe classification.

    Truncates the description to the first ``LLM_MAX_DESCRIPTION_CHARS``
    characters. Any failure, invalid JSON, or low-confidence implementation
    answer downgrades to ``plan_only``.
    """
    truncated = feature_description[:LLM_MAX_DESCRIPTION_CHARS]
    request = LLMRequest(
        messages=[
            Message(role="system", content=_LLM_SYSTEM_PROMPT),
            Message(
                role="user",
                content=_LLM_USER_PROMPT_TEMPLATE.format(text=truncated),
            ),
        ],
        model="",
        temperature=0.0,
        max_output_tokens=LLM_MAX_OUTPUT_TOKENS,
        timeout_seconds=LLM_TIMEOUT_SECONDS,
        response_format="json_object",
    )

    try:
        response = await complete_for_role(Role.TRIAGE, request)
    except (ProviderConfigurationError, UnsupportedProviderError) as error:
        logger.warning(
            "[INTENT] LLM fallback unavailable (no provider); "
            "defaulting to plan_only. error=%s",
            error,
        )
        return PLAN_ONLY, "default", None, "no_provider"
    except Exception as error:
        logger.warning(
            "[INTENT] LLM fallback failed; defaulting to plan_only. error=%s",
            error,
        )
        return PLAN_ONLY, "default", None, "llm_error"

    parsed, parse_error = _parse_llm_intent_payload(response.text or "")
    if parsed is None:
        return PLAN_ONLY, "default", None, parse_error

    llm_intent, confidence, reason = parsed
    if (
        llm_intent == IMPLEMENTATION
        and confidence < LLM_IMPLEMENTATION_MIN_CONFIDENCE
    ):
        return (
            PLAN_ONLY,
            "llm_downgraded",
            confidence,
            "low_confidence_implementation",
        )
    return llm_intent, "llm", confidence, reason


async def classify_intent_async(feature_description: str) -> Intent:
    """
    Hybrid classifier: deterministic layers first, LLM fallback only when
    nothing deterministic matches. Read-only safety blockers are absolute
    and scan the full untruncated description.
    """
    start = time.monotonic()
    text = _normalize(feature_description or "")
    intent, source, matched = _deterministic_classify(text)

    if intent is not None:
        duration_ms = int((time.monotonic() - start) * 1000)
        _log_intent_decision(
            intent=intent,
            source=source,
            matched=matched,
            description_truncated=False,
            duration_ms=duration_ms,
        )
        return intent

    description_truncated = len(feature_description or "") > LLM_MAX_DESCRIPTION_CHARS
    intent, source, confidence, reason = await _llm_fallback_classify(
        feature_description
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    _log_intent_decision(
        intent=intent,
        source=source,
        matched=matched,
        description_truncated=description_truncated,
        duration_ms=duration_ms,
        confidence=confidence,
        reason=reason,
    )
    return intent
