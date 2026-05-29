"""
implementation_guard.py
Deterministic ambiguous-implementation guard (PR #9A).

Guiding principle
-----------------
An implementation request is specific enough only if a developer could start
work WITHOUT inventing the feature, behavior, target file/module, endpoint, UI
area, bug, or acceptance criteria. If acting on the request would force the
model to *define the feature itself*, it is too vague — block it and ask for
clarification before any triage / chunk planning / run creation.

How that principle is encoded deterministically
------------------------------------------------
A request is actionable only if it carries at least one CONCRETE ANCHOR — a
token that actually names the work:
  - a filename / path / extension (README, backend/routes/auth.py)
  - a code identifier (snake_case, CamelCase, dotted, or containing digits)
  - a numeric / config value (timeout 30 -> 60)
  - a concrete domain noun that names a feature/target (password reset, CSV
    export, webhook retry, health check endpoint, login button)

Words that DESCRIBE but never NAME the work are not anchors, no matter how
they are spelled:
  - implementation verbs (implement, build, add, fix, ...)
  - size/scope modifiers (small, big, medium, ...)
  - subjective quality adjectives (extraordinary, amazing, cool, nice, ...)
  - vague quantifiers/determiners (one, some, several, ...)
  - generic head nouns (feature, improvement, change, thing, app, ...)

So "implement one extraordinary feature" blocks — every word merely describes
an unnamed feature — while "implement password reset feature" passes, because
"password reset" names the feature a developer would build. This is deliberately
*not* a list of blocked sentences; it is a small set of word CATEGORIES that
cannot anchor work, plus the rule "specific iff a concrete anchor remains."

This guard is the deterministic first pass. Genuinely ambiguous requests (that
do not deterministically classify as implementation) additionally get the LLM
fallback's specificity verdict in ``intent.py`` — no second LLM call is added.

It is purely deterministic here: no LLM call, no DB access, no run creation.

Typo tolerance: the describe-only words (verbs/modifiers/generic nouns) are
fuzzy-matched (small edit distance) so "implemnt a medum featre" still blocks.
Concrete anchors and subjective adjectives are NEVER fuzzy-matched — technical
identifiers can legitimately look unusual and must not collapse into a vague
word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Public, reusable copy for the needs_clarification response.
NEEDS_CLARIFICATION_MESSAGE = (
    "This request is too vague to implement safely. Please specify the exact "
    "behavior, bug, file/module, endpoint, UI area, or acceptance criteria."
)
DEFAULT_MISSING_DETAILS = [
    "exact behavior or bug",
    "target file/module/endpoint/UI area",
    "acceptance criteria",
]
DEFAULT_EXAMPLES = [
    "fix typo in README",
    "rename login button to Sign in",
    "add health check endpoint",
    "update timeout from 30 to 60 seconds",
]

# Copy for a request that is not a work request at all (greeting / noise). This
# is distinct from a vague *implementation* request: here we do not yet know
# whether the user wants a report, a plan, or an implementation, so the intent
# is reported as "unknown".
NON_ACTIONABLE_MESSAGE = (
    "Please describe what you want Pipewright to do: review the repo, create a "
    "plan, or implement a specific change."
)
NON_ACTIONABLE_MISSING_DETAILS = [
    "whether you want a report, a plan, or an implementation",
    "the target feature, bug, file, endpoint, UI area, or acceptance criteria",
]
NON_ACTIONABLE_EXAMPLES = [
    "explain this project",
    "give me a plan to add login",
    "fix typo in README",
    "add health check endpoint",
]


@dataclass
class ImplementationSpecificityResult:
    """Outcome of the ambiguous-implementation assessment."""

    is_specific_enough: bool
    reason: str
    missing_details: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)


# Vague verbs. A request built only from these (plus modifiers/stopwords) has
# no concrete target. Every implementation verb is here on purpose: the
# specificity must come from the *object* of the verb (a file, endpoint,
# identifier, number, ...), not the verb itself.
_GENERIC_VERBS = {
    "implement", "build", "make", "do", "create", "add", "fix", "change",
    "modify", "update", "refactor", "improve", "enhance", "optimize",
    "clean", "cleanup", "remove", "delete", "replace", "upgrade",
    "integrate", "write", "rename", "adjust", "tweak", "edit", "handle",
}

# Vague size / quality modifiers.
_GENERIC_MODIFIERS = {
    "small", "medium", "sized", "big", "large", "major", "minor", "safe",
    "simple", "new", "tiny", "huge", "quick", "basic", "general", "generic",
    "slight", "better", "best", "good", "nice", "production",
}

# Generic head nouns that, on their own, do not name a concrete change. These
# always demand a concrete specifier ("password reset feature", not just
# "feature").
_GENERIC_NOUNS = {
    "feature", "features", "change", "changes", "improvement", "improvements",
    "enhancement", "enhancements", "update", "updates", "cleanup", "refactor",
    "code", "codebase", "app", "apps", "application", "applications",
    "project", "projects", "repo", "repository", "thing", "things", "stuff",
    "something", "anything", "everything", "nothing", "solution", "solutions",
    "it", "this", "that",
}

# Generic nouns that must be matched EXACTLY (never fuzzily): they sit one edit
# away from real identifiers ("solution"/"solutions" near domain words), so
# fuzzy-matching them would swallow legitimate anchors.
_EXACT_ONLY_NOUNS = {"solution", "solutions"}

# Subjective quality adjectives and vague quantifiers. They describe how good
# or how much, never WHAT — so they cannot anchor work. This is a CATEGORY of
# describe-only words, not a list of blocked phrases. Exact-match only (we do
# not fuzzy-match adjectives — too close to real identifiers like "user").
_VAGUE_QUALIFIERS = {
    # subjective quality adjectives
    "extraordinary", "amazing", "awesome", "cool", "nice", "useful",
    "helpful", "great", "fancy", "powerful", "robust", "modern", "beautiful",
    "neat", "slick", "elegant", "wonderful", "fantastic", "incredible",
    "remarkable", "impressive", "stellar", "superb", "excellent",
    "outstanding", "brilliant", "magnificent", "splendid", "terrific",
    "lovely", "pretty", "intuitive", "seamless", "delightful", "polished",
    "innovative", "creative", "special", "perfect", "ideal", "exciting",
    "fun", "shiny", "clever", "smooth", "ordinary",
    # intensifiers / adverbs that amplify an adjective but name nothing. These
    # also catch split spellings ("extra ordinary", "super cool"), since each
    # fragment is then a describe-only word.
    "extra", "super", "ultra", "mega", "hyper", "uber", "extremely",
    "incredibly", "absolutely", "totally", "highly", "insanely", "wildly",
    # vague quantifiers / determiners that name nothing
    "one", "two", "three", "few", "several", "many", "multiple", "various",
    "couple", "numerous", "countless",
}

# Greeting / noise words. These are blocked when they stand alone but are NOT
# fuzzy targets (so a real word like "text" never collapses into "test").
_GREETINGS = {
    "hi", "hii", "hello", "helo", "hey", "heyy", "yo", "hiya", "sup",
    "test", "testing", "ping", "ok", "okay",
}

# Politeness / filler words used alongside greetings. Combined with greetings
# (and stopwords) these define a "non-actionable" input — a message that is not
# a work request at all (e.g. "hello", "ok thanks", "thank you", "hello bro").
_POLITENESS = {
    "thanks", "thank", "thx", "ty", "cheers", "greetings", "morning",
    "afternoon", "evening", "hola", "hallo", "howdy", "welcome",
}

# Casual filler / address words that carry no work content. They let common
# greetings like "hello bro" / "hey man" stay non-actionable.
_CASUAL_FILLER = {
    "bro", "bros", "dude", "dudes", "man", "buddy", "mate", "pal", "guys",
    "folks", "sir", "madam", "maam", "there", "pls", "plz", "lol", "hmm",
    "hm", "yep", "yeah", "nope", "nah", "huh",
}

_NOISE_WORDS = _GREETINGS | _POLITENESS | _CASUAL_FILLER

_STOPWORDS = {
    "a", "an", "the", "to", "in", "into", "of", "for", "on", "with", "and",
    "or", "my", "our", "your", "please", "can", "could", "would", "will",
    "you", "i", "me", "we", "us", "just", "now", "also", "more", "less",
    "from", "at", "by", "as", "be", "is", "are", "some", "any", "all", "so",
    "up", "out", "about", "over", "around", "really", "very",
}

# Exact-match vocabulary: any token that exactly matches one of these merely
# describes the work (or is noise) and cannot anchor it.
_GENERIC_EXACT = (
    _GENERIC_VERBS
    | _GENERIC_MODIFIERS
    | _GENERIC_NOUNS
    | _VAGUE_QUALIFIERS
    | _GREETINGS
    | _STOPWORDS
)

# Fuzzy-match targets: only the meaningful describe-only words (length >= 4)
# get typo tolerance. Subjective adjectives, quantifiers, greetings, stopwords,
# short words, and collision-prone nouns are exact-only — they sit too close to
# real identifiers to fuzzy-match safely.
_FUZZY_TARGETS = sorted(
    word
    for word in (_GENERIC_VERBS | _GENERIC_MODIFIERS | _GENERIC_NOUNS)
    if len(word) >= 4 and word not in _EXACT_ONLY_NOUNS
)

# Token = a run of word / identifier / path / number characters. Hyphens are
# treated as separators so "medium-sized" splits into "medium" + "sized".
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./]+")


def _normalize(value: str) -> str:
    return " ".join((value or "").lower().replace("’", "'").split())


def _word_tokens(text: str) -> list[str]:
    tokens = []
    for raw in _TOKEN_RE.findall(text):
        token = raw.strip("._/")
        if token:
            tokens.append(token)
    return tokens


def is_non_actionable_request(feature_description: str) -> bool:
    """
    True when the input is not a work request at all — a greeting, a thank-you,
    a "test", or otherwise composed only of noise/politeness/stopwords with no
    actionable content.

    Pure and deterministic; intended to run BEFORE intent classification so a
    greeting never becomes a plan_ready/report_ready run. It is intentionally
    strict: it fires only when EVERY token is noise or a stopword, so any real
    verb or target ("explain this project", "fix login") is left for the normal
    classifier and implementation guard.
    """
    text = _normalize(feature_description)
    tokens = _word_tokens(text)
    if not tokens:
        return True
    return all(token in _NOISE_WORDS or token in _STOPWORDS for token in tokens)


def _levenshtein_within(a: str, b: str, max_distance: int) -> bool:
    """
    Return True if the Levenshtein distance between ``a`` and ``b`` is at most
    ``max_distance``. Early-exits via a length-difference check and a banded
    row computation. Plain Levenshtein (distance 2 covers a single
    transposition like "chnage" -> "change").
    """
    if abs(len(a) - len(b)) > max_distance:
        return False
    if a == b:
        return True

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return False
        previous = current
    return previous[-1] <= max_distance


def _fuzzy_threshold(token_length: int) -> int:
    """Edit-distance budget keyed on token length (conservative)."""
    if token_length <= 3:
        return 0  # exact only — too short to fuzzy-match safely
    if token_length <= 5:
        return 1
    return 2


def _is_vague_word(token: str) -> bool:
    if token in _GENERIC_EXACT:
        return True
    threshold = _fuzzy_threshold(len(token))
    if threshold == 0:
        return False
    for target in _FUZZY_TARGETS:
        if _levenshtein_within(token, target, threshold):
            return True
    return False


def _concrete_tokens(text: str) -> list[str]:
    """
    Return the tokens that act as concrete anchors — words that NAME the work
    rather than merely describe it: filenames, identifiers, numbers, and
    concrete domain nouns. Any token that is (exactly or fuzzily) an
    implementation verb, size/scope modifier, subjective quality adjective,
    vague quantifier, generic head noun, greeting, or stopword is dropped.

    So "the", "improve", "big", "extraordinary", "feature", "one" are not
    anchors; "readme", "timeout", "user_service", "login", "password", "30"
    are.
    """
    concrete: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        token = raw.strip("._/")
        if not token:
            continue
        if _is_vague_word(token):
            continue
        concrete.append(token)
    return concrete


def assess_implementation_specificity(
    feature_description: str,
) -> ImplementationSpecificityResult:
    """
    Decide whether an implementation request is concrete enough to proceed to
    triage/chunk planning. Deterministic; never calls an LLM.

    Principle: a request is specific enough only if a developer could start
    work without inventing the feature/behavior/target/bug/criteria. That holds
    iff the request still contains a concrete anchor (a filename, path, code
    identifier, numeric/config value, or concrete domain noun) after every
    describe-only word — verbs, sizes, subjective adjectives, vague quantifiers,
    and generic head nouns — is stripped. If only describe-only words remain,
    the model would have to define the feature itself, so we block.
    """
    text = _normalize(feature_description)
    if not text:
        return ImplementationSpecificityResult(
            is_specific_enough=False,
            reason="Empty request; nothing concrete to implement.",
            missing_details=list(DEFAULT_MISSING_DETAILS),
            examples=list(DEFAULT_EXAMPLES),
        )

    concrete = _concrete_tokens(text)
    if concrete:
        return ImplementationSpecificityResult(
            is_specific_enough=True,
            reason=(
                "Request contains a concrete anchor: "
                f"{', '.join(concrete[:5])}."
            ),
            missing_details=[],
            examples=[],
        )

    return ImplementationSpecificityResult(
        is_specific_enough=False,
        reason=(
            "Request only describes work without naming it (no file, "
            "identifier, value, or concrete feature/target); implementing it "
            "would require inventing the feature."
        ),
        missing_details=list(DEFAULT_MISSING_DETAILS),
        examples=list(DEFAULT_EXAMPLES),
    )
