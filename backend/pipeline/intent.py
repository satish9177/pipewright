"""
Deterministic request intent classification with an LLM fallback.

Layers (in order):
  1. Read-only safety blockers (absolute; never call LLM). Scope constraints
     ("do not modify OTHER files") and anti-report meta ("do not run as a
     read-only report") are excluded from this layer; a request that both asks
     to implement AND forbids all changes is surfaced as an uncertain
     contradiction rather than a silent report/plan.
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
from dataclasses import dataclass, field
from typing import Literal

from backend.llm import Role, complete_for_role
from backend.llm.base import LLMRequest, Message
from backend.llm.errors import ProviderConfigurationError, UnsupportedProviderError

Intent = Literal["report_only", "plan_only", "implementation"]

REPORT_ONLY: Intent = "report_only"
PLAN_ONLY: Intent = "plan_only"
IMPLEMENTATION: Intent = "implementation"

Specificity = Literal["specific", "needs_clarification"]
SPECIFIC: Specificity = "specific"
NEEDS_CLARIFICATION: Specificity = "needs_clarification"

logger = logging.getLogger(__name__)

LLM_MAX_DESCRIPTION_CHARS = 300
# Higher token budget than before: the LLM now also returns specificity fields
# (still a single call — no extra LLM round-trip is added).
LLM_MAX_OUTPUT_TOKENS = 160
LLM_TIMEOUT_SECONDS = 10
LLM_IMPLEMENTATION_MIN_CONFIDENCE = 0.8
# An LLM "specific" verdict below this confidence is treated as too uncertain
# to safely implement, so the route asks for clarification.
LLM_SPECIFICITY_MIN_CONFIDENCE = 0.75

_ALLOWED_INTENTS = {REPORT_ONLY, PLAN_ONLY, IMPLEMENTATION}
_ALLOWED_SPECIFICITY = {SPECIFIC, NEEDS_CLARIFICATION}


@dataclass
class IntentDecision:
    """
    Full classification result. ``classify_intent_async`` returns only
    ``.intent`` for backward compatibility; the route uses the richer fields
    to decide whether an implementation request is specific enough to proceed.

    The specificity fields are only meaningful when ``from_llm`` is True and
    ``intent == implementation`` — they carry the LLM's specificity verdict
    obtained in the same single fallback call.
    """

    intent: Intent
    source: str
    confidence: float | None = None
    reason: str | None = None
    from_llm: bool = False
    specificity: Specificity | None = None
    specificity_confidence: float | None = None
    clarification_message: str | None = None
    missing_details: list[str] = field(default_factory=list)
    # True when the classifier could not confidently place the request into
    # report_only / plan_only / implementation (LLM error, unparsable output,
    # or a low-confidence implementation guess). The route turns this into a
    # needs_clarification response instead of fabricating a plan_only run.
    # ``.intent`` still carries the legacy safe default (plan_only) for
    # backward-compatible callers of ``classify_intent_async``.
    uncertain: bool = False


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

# Discovery / advisory questions. The user is asking the system to identify
# or suggest possibilities ("what could/should we do?"), not to do a specific
# thing. These are read-only and route to report_only — even when they contain
# an implementation verb like "add"/"build"/"improve" — so they never enter
# chunk planning and never become a needs_clarification dead-end.
_DISCOVERY_PHRASES = [
    "what feature",
    "what features",
    "which feature",
    "which features",
    "what can we add",
    "what can we build",
    "what can we improve",
    "what can be added",
    "what can be improved",
    "what should we add",
    "what should we build",
    "what should we do",
    "what should we work on",
    "what should we build next",
    "what could we add",
    "what could we build",
    "what could we improve",
    "what to add",
    "what to build",
    "what to improve",
    "what improvements",
    "what enhancements",
    "what do you suggest",
    "what would you suggest",
    "what should we build next",
    "what next",
    "what's next",
    "whats next",
    "suggest feature",
    "suggest features",
    "suggest improvement",
    "suggest improvements",
    "suggest some",
    "suggest a few",
    "suggest what",
    "recommend feature",
    "recommend improvement",
    "feature ideas",
    "ideas for",
    "suggestions for",
    "how can we improve",
    "how could we improve",
    "how do we improve",
    "how should we improve",
    "how can we make this better",
    "how can we make it better",
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
    # Non-contiguous "make ... better" (e.g. "make the app better",
    # "make it better"). The contiguous "make better" is also in the phrase
    # list above. This keeps such vague improvement asks on the implementation
    # path, where the specificity guard deterministically blocks them.
    re.compile(r"\bmake\b.+\bbetter\b"),
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

# Object "head" nouns that mean "anything at all". A negated change verb
# applied to one of these is a genuine global read-only instruction.
_GLOBAL_OBJECT_HEADS = {
    "code",
    "codebase",
    "codes",
    "file",
    "files",
    "anything",
    "everything",
    "thing",
    "things",
    "source",
    "sources",
    "it",
    "them",
    "line",
    "lines",
}

# Leading words that qualify an object without changing its head noun.
_LEADING_QUALIFIERS = {
    "the",
    "a",
    "an",
    "any",
    "all",
    "my",
    "your",
    "our",
    "its",
    "this",
    "these",
    "those",
    "single",
    "existing",
}

# Anti-report meta: text that *references* read-only / report mode in order to
# REJECT it ("do not run as a read-only report", "this is not read-only").
# These are stripped before read-only scanning so a substring like "read-only"
# inside an anti-report sentence never trips the blocker.
_ANTI_REPORT_META_REGEXES = [
    re.compile(
        r"(?:do not|don't|dont)\s+run\s+(?:this\s+|it\s+)?as\s+(?:a\s+)?"
        r"read[\s-]?only(?:\s+report)?"
    ),
    re.compile(r"not\s+(?:a\s+)?read[\s-]?only(?:\s+report)?"),
]

# Scope constraints: "do not touch OTHER files" style limits on an
# implementation. They scope *where* edits may land; they are NOT a global
# "change nothing" instruction. Stripped before read-only scanning so the
# negated verb ("do not modify") inside them is not read as global read-only.
_SCOPE_CONSTRAINT_REGEXES = [
    re.compile(
        r"(?:do not|don't|dont)\s+"
        r"(?:change|modify|edit|alter|touch|create|add|delete|remove|update|write)"
        r"(?:\s+or\s+"
        r"(?:change|modify|edit|alter|touch|create|add|delete|remove|update|write))?"
        r"\s+(?:any\s+)?other\s+"
        r"(?:files?|code|modules?|things?|places?|parts?|paths?)"
    ),
    re.compile(
        r"only\s+(?:touch|modify|change|edit|update|create)\s+these\s+"
        r"(?:files?|paths?|modules?)"
    ),
]

# "without changing X" is a scope constraint UNLESS X is a global object
# ("code" / "anything" / ...), in which case it is a genuine read-only
# instruction. The replacer keeps global forms and strips scoped ones.
_WITHOUT_SCOPE_RE = re.compile(
    r"without\s+(?:changing|modifying|editing|altering)\s+"
    r"([a-z][\w./-]*(?:\s+[a-z][\w./-]*)?)"
)

# Global read-only: a negated change verb applied to a global object. Catches
# forms the curated phrase list misses ("do not change any code", "do not
# modify any files"). "other" objects are excluded (handled as scope above).
_GLOBAL_READONLY_REGEXES = [
    re.compile(
        r"(?:do not|don't|dont)\s+"
        r"(?:change|modify|edit|alter|touch|rewrite|update|write|add|create|"
        r"delete|remove|implement|fix|improve|optimize|refactor)"
        r"(?:\s+or\s+\w+)?\s+"
        r"(?:any\s+|the\s+|all\s+|any\s+single\s+)?"
        r"(?:code|codebase|files?|anything|everything|thing|source|lines?|"
        r"single\s+line)\b"
    ),
]

# Source label for the deterministic "implement, but do not change anything"
# contradiction. The async classifier turns this into an uncertain decision so
# the route asks for clarification rather than guessing.
_CONTRADICTION_SOURCE = "deterministic_contradiction"

CONTRADICTION_CLARIFICATION_MESSAGE = (
    "This request asks for an implementation but also says not to change any "
    "code. Please clarify whether Pipewright should implement the change or "
    "only analyze it without modifying code."
)

_LLM_SYSTEM_PROMPT = (
    "You classify a single software-engineering request into exactly one of "
    "three intents, and judge how specific an implementation request is:\n"
    "  - report_only: the user wants information, explanation, audit, or "
    "diagnosis only; no code changes.\n"
    "  - plan_only: the user wants a plan, design, or approach; no code "
    "changes.\n"
    "  - implementation: the user wants code to be written or modified.\n"
    "For implementation requests, also judge specificity:\n"
    "  - specific: names concrete behavior, a target (file/module/endpoint/UI "
    "area), an identifier, a value, or acceptance criteria.\n"
    "  - needs_clarification: too vague to implement safely (e.g. 'implement "
    "a big feature', 'make auth better', 'clean up the code', 'fix dashboard "
    "issue').\n"
    "Respond with strict JSON only, matching this schema:\n"
    '  {"intent": "report_only|plan_only|implementation",\n'
    '   "confidence": 0.0-1.0,\n'
    '   "specificity": "specific|needs_clarification",\n'
    '   "specificity_confidence": 0.0-1.0,\n'
    '   "reason": "one short sentence",\n'
    '   "clarification_message": "one sentence or null",\n'
    '   "missing_details": ["short phrases of what is missing"]}\n'
    "For report_only and plan_only, set specificity to \"specific\". "
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


def _strip_leading_qualifiers(phrase: str) -> str:
    words = phrase.split()
    while words and words[0] in _LEADING_QUALIFIERS:
        words = words[1:]
    return " ".join(words)


def _is_global_object(phrase: str) -> bool:
    """
    True when the object of a "without changing X" clause means "anything"
    (e.g. "code", "the codebase") rather than a scoped target (e.g. "public
    API"). An empty/qualifier-only object degrades to global (read-only).
    """
    stripped = _strip_leading_qualifiers(phrase)
    if not stripped:
        return True
    return stripped.split()[0] in _GLOBAL_OBJECT_HEADS


def _strip_scope_and_meta(text: str) -> str:
    """
    Remove anti-report meta references and scope-constraint clauses so they are
    not read as a global read-only instruction.

    Used only for read-only scanning — never for positive intent detection, so
    stripping a scope clause cannot erase a genuine implementation verb from the
    request as a whole.
    """
    out = text
    for pattern in _ANTI_REPORT_META_REGEXES:
        out = pattern.sub(" ", out)
    for pattern in _SCOPE_CONSTRAINT_REGEXES:
        out = pattern.sub(" ", out)

    def _replace_without(match: re.Match[str]) -> str:
        return match.group(0) if _is_global_object(match.group(1)) else " "

    return _WITHOUT_SCOPE_RE.sub(_replace_without, out)


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

    # Read-only detection runs on text with scope constraints and anti-report
    # meta removed, so "do not modify OTHER files" (a scope limit) and "do not
    # run as a read-only report" (an anti-report instruction) are not misread
    # as a global "change nothing" request.
    readonly_text = _strip_scope_and_meta(text)
    has_curated_safety, curated_safety = _contains_any(
        readonly_text, _READ_ONLY_SAFETY_PHRASES
    )
    has_global_regex, global_safety = _matches_any_regex(
        readonly_text, _GLOBAL_READONLY_REGEXES
    )
    has_safety = has_curated_safety or has_global_regex
    safety_matches = curated_safety + global_safety

    has_discovery, discovery_matches = _contains_any(text, _DISCOVERY_PHRASES)
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
    if has_discovery:
        matched["discovery"] = discovery_matches
    if has_report:
        matched["report"] = report_matches
    if has_strong_plan:
        matched["strong_plan"] = strong_plan_matches
    if has_soft_plan:
        matched["soft_plan"] = soft_plan_matches
    if has_implementation:
        matched["implementation"] = impl_matches

    if has_safety:
        # An explicit plan request beats the read-only blocker (existing rule:
        # "plan how to add X, do not implement yet" is a plan request).
        if has_strong_plan or has_soft_plan:
            return PLAN_ONLY, "deterministic_blocker", matched
        # Distinguish a genuine implementation imperative (a contradiction the
        # human must resolve) from an implementation verb that only appears
        # inside the read-only clause itself ("don't *change* code"). Strip the
        # matched read-only phrases; an implementation verb that survives is a
        # real, separate request to write code.
        residual = readonly_text
        for phrase in safety_matches:
            residual = residual.replace(phrase, " ")
        residual_impl, _ = _contains_any(residual, _IMPLEMENTATION_PHRASES)
        residual_impl_rx, _ = _matches_any_regex(residual, _IMPLEMENTATION_REGEXES)
        if residual_impl or residual_impl_rx:
            return PLAN_ONLY, _CONTRADICTION_SOURCE, matched
        return REPORT_ONLY, "deterministic_blocker", matched

    # Discovery / advisory questions are read-only and beat implementation
    # verbs ("what features can we add" is a question, not an "add" request),
    # but an explicit plan request still wins ("give me a plan for ...").
    if has_discovery and not has_strong_plan:
        return REPORT_ONLY, "deterministic_discovery", matched

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


def _coerce_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if not (0.0 <= confidence <= 1.0):
        return None
    return confidence


def _parse_llm_specificity(parsed: dict) -> dict:
    """
    Extract the optional specificity fields leniently. Missing or malformed
    fields degrade to ``None``/empty rather than failing the whole payload, so
    a provider that ignores the specificity contract still yields a valid
    intent classification (preserving PR #6 behavior).
    """
    raw_spec = parsed.get("specificity")
    specificity = raw_spec if raw_spec in _ALLOWED_SPECIFICITY else None

    specificity_confidence = _coerce_confidence(
        parsed.get("specificity_confidence")
    )

    raw_message = parsed.get("clarification_message")
    clarification_message = raw_message if isinstance(raw_message, str) else None

    raw_missing = parsed.get("missing_details")
    if isinstance(raw_missing, list):
        missing_details = [item for item in raw_missing if isinstance(item, str)]
    else:
        missing_details = []

    return {
        "specificity": specificity,
        "specificity_confidence": specificity_confidence,
        "clarification_message": clarification_message,
        "missing_details": missing_details,
    }


def _parse_llm_intent_payload(
    text: str,
) -> tuple[dict | None, str | None]:
    """
    Parse an LLM payload into intent/confidence/reason plus optional
    specificity fields.

    Returns ``(parsed_fields, error_code)``. On success ``error_code`` is
    ``None``. On failure ``parsed_fields`` is ``None`` and ``error_code`` is
    one of ``"invalid_json"`` or ``"invalid_schema"``. The intent and
    confidence requirements are unchanged from PR #6; specificity fields are
    optional and never cause a parse failure.
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

    confidence = _coerce_confidence(parsed.get("confidence"))
    if confidence is None:
        return None, "invalid_schema"

    reason = parsed.get("reason")
    reason_text = reason if isinstance(reason, str) else None

    fields = {
        "intent": raw_intent,
        "confidence": confidence,
        "reason": reason_text,
    }
    fields.update(_parse_llm_specificity(parsed))
    return fields, None


async def _llm_fallback_classify(
    feature_description: str,
) -> IntentDecision:
    """
    Call the LLM for an ambiguous request and return a safe classification.

    Truncates the description to the first ``LLM_MAX_DESCRIPTION_CHARS``
    characters. Any failure, invalid JSON, or low-confidence implementation
    answer downgrades to ``plan_only``. A single call also yields the
    implementation specificity verdict — no extra LLM round-trip is added.
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
        return IntentDecision(
            intent=PLAN_ONLY, source="default", reason="no_provider",
            from_llm=True, uncertain=True,
        )
    except Exception as error:
        logger.warning(
            "[INTENT] LLM fallback failed; defaulting to plan_only. error=%s",
            error,
        )
        return IntentDecision(
            intent=PLAN_ONLY, source="default", reason="llm_error",
            from_llm=True, uncertain=True,
        )

    parsed, parse_error = _parse_llm_intent_payload(response.text or "")
    if parsed is None:
        return IntentDecision(
            intent=PLAN_ONLY, source="default", reason=parse_error,
            from_llm=True, uncertain=True,
        )

    llm_intent: Intent = parsed["intent"]
    confidence: float = parsed["confidence"]
    if (
        llm_intent == IMPLEMENTATION
        and confidence < LLM_IMPLEMENTATION_MIN_CONFIDENCE
    ):
        return IntentDecision(
            intent=PLAN_ONLY,
            source="llm_downgraded",
            confidence=confidence,
            reason="low_confidence_implementation",
            from_llm=True,
            uncertain=True,
        )
    return IntentDecision(
        intent=llm_intent,
        source="llm",
        confidence=confidence,
        reason=parsed["reason"],
        from_llm=True,
        specificity=parsed["specificity"],
        specificity_confidence=parsed["specificity_confidence"],
        clarification_message=parsed["clarification_message"],
        missing_details=parsed["missing_details"],
    )


async def classify_intent_details_async(feature_description: str) -> IntentDecision:
    """
    Hybrid classifier returning the full decision (intent + specificity).

    Deterministic layers run first; the LLM fallback runs only when nothing
    deterministic matches, and that single call now also returns the
    implementation specificity verdict. Read-only safety blockers are absolute
    and scan the full untruncated description.
    """
    start = time.monotonic()
    text = _normalize(feature_description or "")
    intent, source, matched = _deterministic_classify(text)

    if intent is not None:
        # A deterministic contradiction ("implement X, but do not change any
        # code") is not safe to route as plan_only silently — surface it as
        # uncertain so the route asks for clarification. ``.intent`` keeps the
        # safe plan_only default for legacy callers of the sync classifier.
        uncertain = source == _CONTRADICTION_SOURCE
        duration_ms = int((time.monotonic() - start) * 1000)
        _log_intent_decision(
            intent=intent,
            source=source,
            matched=matched,
            description_truncated=False,
            duration_ms=duration_ms,
        )
        return IntentDecision(
            intent=intent,
            source=source,
            from_llm=False,
            uncertain=uncertain,
            clarification_message=(
                CONTRADICTION_CLARIFICATION_MESSAGE if uncertain else None
            ),
        )

    description_truncated = len(feature_description or "") > LLM_MAX_DESCRIPTION_CHARS
    decision = await _llm_fallback_classify(feature_description)
    duration_ms = int((time.monotonic() - start) * 1000)
    _log_intent_decision(
        intent=decision.intent,
        source=decision.source,
        matched=matched,
        description_truncated=description_truncated,
        duration_ms=duration_ms,
        confidence=decision.confidence,
        reason=decision.reason,
    )
    return decision


async def classify_intent_async(feature_description: str) -> Intent:
    """
    Backward-compatible hybrid classifier returning only the intent.

    Thin wrapper over :func:`classify_intent_details_async`.
    """
    decision = await classify_intent_details_async(feature_description)
    return decision.intent
