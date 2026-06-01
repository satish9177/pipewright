"""
Tests for the deterministic clarification selection parser (PR #17L).

Pure unit tests. No DB, no FastAPI, no disk, no LLM.
"""

import pytest

from backend.pipeline.clarification_selection import (
    SelectionStatus,
    parse_clarification_selection,
)

pytestmark = pytest.mark.unit


CANDIDATES = [
    "README.md",
    "docs/adr/README.md",
    "docs/architecture/README.md",
]
RECOMMENDED = "README.md"


def _selected(reply, recommended=RECOMMENDED):
    result = parse_clarification_selection(reply, CANDIDATES, recommended)
    assert result.status is SelectionStatus.SELECTED, result
    return result.selected_path


def _unrecognized(reply, recommended=RECOMMENDED):
    result = parse_clarification_selection(reply, CANDIDATES, recommended)
    assert result.status is SelectionStatus.UNRECOGNIZED, result
    assert result.selected_path is None
    return result


# 1. Numeric selection ------------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    ["1", "#1", "1.", "option 1", "use 1", "yes 1", "select 1"],
)
def test_index_one_variants_select_first(reply):
    assert _selected(reply) == "README.md"


def test_index_two_selects_second():
    assert _selected("2") == "docs/adr/README.md"


def test_index_three_selects_third():
    assert _selected("3") == "docs/architecture/README.md"


def test_index_with_leading_words_and_punctuation():
    assert _selected("option 3.") == "docs/architecture/README.md"


# 2. Out of range -----------------------------------------------------------

@pytest.mark.parametrize("reply", ["0", "4", "9"])
def test_out_of_range_index_is_unrecognized(reply):
    _unrecognized(reply)


# 3. Path selection ---------------------------------------------------------

def test_exact_path_selects():
    assert _selected("README.md") == "README.md"


def test_use_exact_path_selects():
    assert _selected("use README.md") == "README.md"


def test_nested_exact_path_selects():
    assert _selected("docs/adr/README.md") == "docs/adr/README.md"


def test_path_not_in_candidates_is_unrecognized():
    _unrecognized("docs/other/README.md")


def test_wrong_case_path_is_unrecognized():
    # Case-sensitive on purpose: the caller re-clarifies rather than guess.
    _unrecognized("readme.md")


# 4. Affirmation ------------------------------------------------------------

def test_yes_selects_recommended_when_present():
    assert _selected("yes") == "README.md"


def test_confirm_selects_recommended_when_present():
    assert _selected("confirm") == "README.md"


def test_yes_is_unrecognized_when_no_recommendation():
    _unrecognized("yes", recommended=None)


def test_confirm_is_unrecognized_when_no_recommendation():
    _unrecognized("confirm", recommended=None)


def test_yes_with_index_still_uses_index_not_recommendation():
    # "yes 2" must pick candidate 2, not the recommended path.
    assert _selected("yes 2") == "docs/adr/README.md"


# 5. Ambiguous / free text --------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    ["2 or 3", "both", "the main one", "", "   ", "asdf!!", "option"],
)
def test_ambiguous_or_garbage_is_unrecognized(reply):
    _unrecognized(reply)


# 6. Purity / determinism ---------------------------------------------------

def test_same_args_same_result():
    first = parse_clarification_selection("yes 1", CANDIDATES, RECOMMENDED)
    second = parse_clarification_selection("yes 1", CANDIDATES, RECOMMENDED)
    assert first == second


def test_candidates_input_not_mutated():
    candidates = list(CANDIDATES)
    snapshot = list(candidates)
    parse_clarification_selection("2", candidates, RECOMMENDED)
    parse_clarification_selection("nonsense", candidates, RECOMMENDED)
    assert candidates == snapshot


def test_index_meaning_depends_on_passed_candidates():
    # "1" only means whatever the explicitly-passed candidate list says.
    other = ["only/one.md"]
    result = parse_clarification_selection("1", other, None)
    assert result.status is SelectionStatus.SELECTED
    assert result.selected_path == "only/one.md"


def test_index_unrecognized_against_shorter_list():
    result = parse_clarification_selection("3", ["a.md", "b.md"], None)
    assert result.status is SelectionStatus.UNRECOGNIZED
