"""
test_implementation_guard.py
Unit tests for the deterministic ambiguous-implementation guard (PR #9A).

The guard encodes one principle: a request is specific enough only if a
developer could start work WITHOUT inventing the feature/behavior/target/bug/
criteria. These tests assert the principle on word CATEGORIES, not on a fixed
list of blocked sentences — see ``PRINCIPLE_*`` cases for novel phrasings that
are not in the original examples.

Pure function tests: no DB, no LLM, no run creation.
"""

import pytest

from backend.pipeline.implementation_guard import (
    DEFAULT_EXAMPLES,
    DEFAULT_MISSING_DETAILS,
    assess_implementation_specificity,
    is_non_actionable_request,
)

pytestmark = pytest.mark.unit


VAGUE_REQUESTS = [
    # Describe-only: verb + size/quality + generic head noun, nothing named.
    "implement a small safe change",
    "implement a medium feature",
    "implement a medium-sized feature",
    "implement a big feature",
    "implement a large feature",
    "implement a major change",
    "add a feature",
    "add hello",
    "build a new feature",
    "make some improvement",
    "do a large update",
    "fix it",
    "fix something",
    "rename thing",
    "update something",
    "change the file",
    "change the code",
    "improve backend",
    "improve the app",
    "make it better",
    "make UI good",
    "do some cleanup",
    # Subjective quality adjectives / vague quantifiers cannot anchor work.
    "implement one extraordinary feature",
    "implement an amazing feature",
    "build something useful",
    "create a cool feature",
    "add a nice improvement",
    "make the app better",
    # Greetings / noise.
    "hi",
    "hello",
    "hey",
    "test",
    # Typo tolerance: describe-only words only.
    "implemnt a medum featre",
    "make smal improvmnt",
    "do some enhancment",
    "chnage the code",
    "fix somthing",
]

SPECIFIC_REQUESTS = [
    # Each NAMES the work: a file, identifier, value, or concrete domain noun.
    "implement password reset feature",
    "add CSV export feature",
    "add webhook retry feature",
    "add health check endpoint",
    "add hello in the readme",
    "fix typo in README",
    "rename readme",
    "rename README.md",
    "change Login button text to Sign in",
    "update timeout from 30 to 60 seconds",
    "implement login feature",
    "build password reset flow",
    "fix NullPointer in UserService",
    "fix validation error in webhook secret check",
]

# Principle, not sentence matching: these phrasings are deliberately absent
# from the example lists above. A novel subjective adjective + generic noun
# must still block; a novel concrete target must still pass.
PRINCIPLE_BLOCK = [
    "implement a brilliant feature",
    "create an impressive improvement",
    "build a fantastic app",
    "add a slick thing",
    "make it awesome",
    "build several powerful things",
    "implement a stellar solution",
]

PRINCIPLE_ALLOW = [
    "fix race condition in scheduler",
    "add pagination to results list",
    "implement dark mode toggle",
    "update retry limit from 3 to 5",
    "add OAuth login support",
    "fix 500 error on checkout",
]

# Split / hyphenated subjective adjectives must still block — each fragment is
# a describe-only word.
SPLIT_ADJECTIVE_BLOCK = [
    "implement one extra ordinary feature",
    "implement one extra-ordinary feature",
    "implement one extraordinary feature",
    "implement a super cool feature",
    "implement a very useful feature",
]

# Non-work inputs: greetings / politeness / noise.
NON_ACTIONABLE_INPUTS = [
    "hello",
    "hi",
    "hey",
    "yo",
    "test",
    "ok",
    "okay",
    "thanks",
    "thank you",
    "",
    "   ",
]

# Real requests must NOT be treated as non-actionable.
ACTIONABLE_INPUTS = [
    "explain this project",
    "give me a plan to add login",
    "implement login feature",
    "add CSV export feature",
    "add health check endpoint",
    "fix typo in README",
    "update retry limit from 3 to 5",
    "fix it",  # vague, but still a work request (handled by the impl guard)
]


@pytest.mark.parametrize("request_text", VAGUE_REQUESTS)
def test_vague_requests_are_blocked(request_text):
    result = assess_implementation_specificity(request_text)
    assert result.is_specific_enough is False
    assert result.reason
    assert result.missing_details == DEFAULT_MISSING_DETAILS
    assert result.examples == DEFAULT_EXAMPLES


@pytest.mark.parametrize("request_text", SPECIFIC_REQUESTS)
def test_specific_requests_are_allowed(request_text):
    result = assess_implementation_specificity(request_text)
    assert result.is_specific_enough is True
    assert result.missing_details == []
    assert result.examples == []


@pytest.mark.parametrize("request_text", PRINCIPLE_BLOCK)
def test_describe_only_requests_block_even_when_phrasing_is_novel(request_text):
    # The model would have to invent the feature, so block — regardless of how
    # flattering the adjective is.
    assert assess_implementation_specificity(request_text).is_specific_enough is False


@pytest.mark.parametrize("request_text", PRINCIPLE_ALLOW)
def test_named_work_passes_even_when_phrasing_is_novel(request_text):
    # A developer could start without inventing the feature, so pass.
    assert assess_implementation_specificity(request_text).is_specific_enough is True


@pytest.mark.parametrize("adjective", [
    "extraordinary", "amazing", "cool", "nice", "useful", "powerful",
    "elegant", "fantastic", "stellar", "impressive",
])
def test_subjective_adjective_does_not_anchor_a_generic_noun(adjective):
    # "<verb> a <subjective adjective> feature" names nothing -> block.
    result = assess_implementation_specificity(f"implement a {adjective} feature")
    assert result.is_specific_enough is False


@pytest.mark.parametrize("request_text", SPLIT_ADJECTIVE_BLOCK)
def test_split_or_hyphenated_adjective_still_blocks(request_text):
    assert assess_implementation_specificity(request_text).is_specific_enough is False


@pytest.mark.parametrize("request_text", NON_ACTIONABLE_INPUTS)
def test_non_actionable_inputs_detected(request_text):
    assert is_non_actionable_request(request_text) is True


@pytest.mark.parametrize("request_text", ACTIONABLE_INPUTS)
def test_real_requests_are_not_non_actionable(request_text):
    assert is_non_actionable_request(request_text) is False


def test_empty_or_whitespace_is_blocked():
    for value in ["", "   ", "\n\t "]:
        result = assess_implementation_specificity(value)
        assert result.is_specific_enough is False


def test_numeric_value_is_a_concrete_anchor():
    result = assess_implementation_specificity("change retries from 3 to 5")
    assert result.is_specific_enough is True


def test_filename_is_a_concrete_anchor():
    result = assess_implementation_specificity("update backend/routes/auth.py")
    assert result.is_specific_enough is True


def test_identifier_is_a_concrete_anchor():
    result = assess_implementation_specificity("fix validateToken")
    assert result.is_specific_enough is True


def test_generic_request_with_only_stopwords_and_verbs_is_blocked():
    result = assess_implementation_specificity(
        "please just make some small improvements to the app"
    )
    assert result.is_specific_enough is False


def test_bare_feature_noun_is_generic():
    # "feature" alone is generic and must not pass.
    assert assess_implementation_specificity("add a feature").is_specific_enough is False


@pytest.mark.parametrize("request_text", [
    "implement login feature",
    "add CSV export feature",
    "add webhook retry feature",
    "implement password reset feature",
])
def test_feature_with_concrete_specifier_passes(request_text):
    # "feature" is a generic head noun, but the concrete specifier (login /
    # CSV export / webhook retry / password reset) names the work.
    assert assess_implementation_specificity(request_text).is_specific_enough is True


def test_concrete_identifier_not_fuzzy_matched_to_generic():
    # "text" must not collapse into the generic noise word "test".
    result = assess_implementation_specificity("update header text label")
    assert result.is_specific_enough is True


def test_readme_survives_fuzzy_generic_verb_stripping():
    result = assess_implementation_specificity("add hello in the readme")

    assert result.is_specific_enough is True
    assert "readme" in result.reason
    assert "rename" not in result.reason
