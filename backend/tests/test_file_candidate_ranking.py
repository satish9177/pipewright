"""
test_file_candidate_ranking.py
Unit tests for the pure ambiguous file candidate ranker (PR #17I).
"""

import pytest

from backend.pipeline.file_candidate_ranking import (
    Recommendation,
    rank_ambiguous_file_candidates,
)

pytestmark = pytest.mark.unit


README_CANDIDATES = [
    "README.md",
    "docs/adr/README.md",
    "docs/architecture/README.md",
]


def _assert_permutation(result, candidates):
    assert set(result.ordered) == set(candidates)
    assert len(result.ordered) == len(candidates)


def test_main_readme_with_negations_recommends_root_readme_strongly():
    result = rank_ambiguous_file_candidates(
        "add hello in the main readme docs not in the architecture or adr readme",
        README_CANDIDATES,
    )

    assert result.recommended == "README.md"
    assert result.recommendation is Recommendation.STRONG
    assert result.ordered[0] == "README.md"
    assert "root-level" in result.reason_tokens
    assert "excluded:adr" in result.reason_tokens
    assert "excluded:architecture" in result.reason_tokens
    _assert_permutation(result, README_CANDIDATES)


def test_positive_docs_adr_recommends_adr_readme_strongly():
    result = rank_ambiguous_file_candidates(
        "add hello in docs adr readme",
        README_CANDIDATES,
    )

    assert result.recommended == "docs/adr/README.md"
    assert result.recommendation is Recommendation.STRONG
    assert result.ordered[0] == "docs/adr/README.md"
    assert "matched:adr" in result.reason_tokens
    assert "matched:docs" in result.reason_tokens
    _assert_permutation(result, README_CANDIDATES)


def test_positive_architecture_recommends_architecture_readme_strongly():
    result = rank_ambiguous_file_candidates(
        "add hello in architecture readme",
        README_CANDIDATES,
    )

    assert result.recommended == "docs/architecture/README.md"
    assert result.recommendation is Recommendation.STRONG
    assert result.ordered[0] == "docs/architecture/README.md"
    assert "matched:architecture" in result.reason_tokens
    _assert_permutation(result, README_CANDIDATES)


def test_plain_readme_does_not_make_strong_recommendation():
    result = rank_ambiguous_file_candidates(
        "add hello in readme",
        README_CANDIDATES,
    )

    assert result.recommendation in {Recommendation.NONE, Recommendation.WEAK}
    assert result.recommendation is not Recommendation.STRONG
    _assert_permutation(result, README_CANDIDATES)


def test_all_negated_returns_sorted_candidates_and_no_recommendation():
    result = rank_ambiguous_file_candidates(
        (
            "not README.md or docs/adr/README.md or "
            "docs/architecture/README.md"
        ),
        README_CANDIDATES,
    )

    assert result.recommended is None
    assert result.recommendation is Recommendation.NONE
    assert result.ordered == tuple(sorted(README_CANDIDATES))
    _assert_permutation(result, README_CANDIDATES)


def test_equal_top_scores_return_no_recommendation():
    candidates = ["docs/adr/README.md", "docs/api/README.md"]
    result = rank_ambiguous_file_candidates(
        "add hello in docs readme",
        candidates,
    )

    assert result.recommended is None
    assert result.recommendation is Recommendation.NONE
    _assert_permutation(result, candidates)


@pytest.mark.parametrize("request_text", [
    "add hello in the main readme docs not in the architecture or adr readme",
    "add hello in docs adr readme",
    "add hello in architecture readme",
    "add hello in readme",
])
def test_ordered_is_always_input_permutation(request_text):
    result = rank_ambiguous_file_candidates(request_text, README_CANDIDATES)
    _assert_permutation(result, README_CANDIDATES)


def test_repeated_calls_are_deterministic():
    first = rank_ambiguous_file_candidates(
        "add hello in docs adr readme",
        README_CANDIDATES,
    )
    second = rank_ambiguous_file_candidates(
        "add hello in docs adr readme",
        README_CANDIDATES,
    )

    assert first == second


def test_input_candidates_are_not_mutated():
    candidates = list(README_CANDIDATES)
    original = list(candidates)

    rank_ambiguous_file_candidates(
        "add hello in the main readme not adr",
        candidates,
    )

    assert candidates == original


def test_empty_candidates_returns_none_and_empty_order():
    result = rank_ambiguous_file_candidates("add hello in readme", [])

    assert result.ordered == ()
    assert result.recommended is None
    assert result.recommendation is Recommendation.NONE
    assert result.reason_tokens == ()


def test_single_candidate_returns_single_deterministic_result():
    result = rank_ambiguous_file_candidates(
        "add hello in readme",
        ["README.md"],
    )

    assert result.ordered == ("README.md",)
    assert result.recommended == "README.md"
    assert result.recommendation is Recommendation.STRONG


def test_negation_is_generic_not_hardcoded_to_adr_or_architecture():
    candidates = [
        "README.md",
        "docs/decisions/README.md",
        "docs/design/README.md",
    ]
    result = rank_ambiguous_file_candidates(
        "add hello in main readme not decisions",
        candidates,
    )

    assert result.recommended == "README.md"
    assert result.recommendation is Recommendation.STRONG
    assert result.ordered[-1] == "docs/decisions/README.md"
    assert "excluded:decisions" in result.reason_tokens
    _assert_permutation(result, candidates)


def test_more_than_three_candidates_are_all_preserved_and_ranked():
    candidates = [
        "README.md",
        "docs/adr/README.md",
        "docs/architecture/README.md",
        "packages/app/README.md",
        "docs/design/README.md",
    ]
    result = rank_ambiguous_file_candidates(
        "add hello in docs design readme",
        candidates,
    )

    assert result.ordered[0] == "docs/design/README.md"
    assert result.recommended == "docs/design/README.md"
    assert result.recommendation is Recommendation.STRONG
    _assert_permutation(result, candidates)
