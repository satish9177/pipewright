"""
test_memory_trust.py
Unit tests for the pure memory trust-helper foundations (M3B).

Pure helpers: no DB, no filesystem, no network, no LLM. These verify
deterministic candidate flagging and reality classification, and assert the
helpers never mutate their inputs and never need a DB/runtime import.
"""

from pathlib import Path

import pytest

from backend.memory import memory_trust as mt

pytestmark = pytest.mark.unit


def _item(item_id, content):
    return {"id": item_id, "content": content}


# --- Near-duplicate detection ----------------------------------------------

def test_exact_normalized_duplicate_is_flagged_exact():
    items = [
        _item("a", "Tests use pytest."),
        _item("b", "tests   use   PYTEST."),  # same after normalization
    ]
    candidates = mt.find_duplicate_candidates(items)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.relation == mt.DUP_EXACT
    assert cand.similarity == 1.0
    assert {cand.left_ref, cand.right_ref} == {"a", "b"}
    assert "identical" in cand.reason.lower()


def test_near_duplicate_phrasings_are_flagged():
    items = [
        _item("a", "Tests use pytest."),
        _item("b", "Run tests with pytest."),
        _item("c", "The project test runner is pytest."),
    ]
    candidates = mt.find_duplicate_candidates(items)
    pairs = {frozenset((c.left_ref, c.right_ref)) for c in candidates}
    # All three describe "tests run via pytest" and should pair up.
    assert frozenset(("a", "b")) in pairs
    assert frozenset(("a", "c")) in pairs
    assert frozenset(("b", "c")) in pairs
    for cand in candidates:
        assert cand.relation in (mt.DUP_EXACT, mt.DUP_NEAR)
        assert "pytest" in cand.shared_tokens


def test_distinct_facts_are_not_flagged():
    items = [
        _item("a", "Backend uses FastAPI."),
        _item("b", "Frontend uses React."),
        _item("c", "Deploy with Docker Compose."),
    ]
    assert mt.find_duplicate_candidates(items) == []


def test_different_test_runners_are_not_duplicates():
    # Contradiction, not a duplicate: must NOT be flagged by the dup detector.
    items = [
        _item("a", "Tests use pytest."),
        _item("b", "Tests use jest."),
    ]
    assert mt.find_duplicate_candidates(items) == []


def test_negation_polarity_prevents_false_duplicate():
    # Stopword removal must not collapse opposite-polarity safety rules.
    items = [
        _item("a", "Never log secrets."),
        _item("b", "Always log secrets."),
    ]
    assert mt.find_duplicate_candidates(items) == []


def test_threshold_is_respected():
    items = [
        _item("a", "Backend uses FastAPI and SQLAlchemy on SQLite."),
        _item("b", "Backend uses FastAPI."),
    ]
    # High threshold => no flag; low threshold => flagged. Deterministic either way.
    assert mt.find_duplicate_candidates(items, threshold=0.95) == []
    loose = mt.find_duplicate_candidates(items, threshold=0.4)
    assert len(loose) == 1
    assert loose[0].relation == mt.DUP_NEAR


def test_duplicate_similarity_is_symmetric_and_bounded():
    a = "Run tests with pytest."
    b = "Tests use pytest."
    sim_ab = mt.duplicate_similarity(a, b)
    sim_ba = mt.duplicate_similarity(b, a)
    assert sim_ab == sim_ba
    assert 0.0 <= sim_ab <= 1.0


def test_items_without_usable_content_are_skipped():
    items = [
        _item("a", "Tests use pytest."),
        {"id": "b"},               # no content
        {"id": "c", "content": "   "},  # blank
        {"id": "d", "content": 123},    # non-string
    ]
    # Only one usable item => no pairs => no candidates, and no error.
    assert mt.find_duplicate_candidates(items) == []


def test_missing_id_falls_back_to_index_ref():
    items = [
        {"content": "Tests use pytest."},
        {"content": "Run tests with pytest."},
    ]
    candidates = mt.find_duplicate_candidates(items)
    assert len(candidates) == 1
    assert {candidates[0].left_ref, candidates[0].right_ref} == {"index:0", "index:1"}


# --- Reality dimension value extraction ------------------------------------

def test_extract_single_value():
    assert mt.extract_dimension_values("test_runner", "Tests use pytest.") == {"pytest"}
    assert mt.extract_dimension_values("backend_framework", "Backend uses FastAPI.") == {"fastapi"}
    assert mt.extract_dimension_values("db_engine", "We use Postgres here.") == {"postgresql"}


def test_extract_is_word_bounded_for_pip():
    # "pipenv"/"pipeline" must not be read as the "pip" package manager.
    assert mt.extract_dimension_values("package_manager", "We use pipenv.") == {"pipenv"}
    assert mt.extract_dimension_values("package_manager", "The pipeline runs nightly.") == set()
    assert mt.extract_dimension_values("package_manager", "Install via pip.") == {"pip"}


def test_extract_compound_token():
    assert mt.extract_dimension_values("test_runner", "Use go test for the API.") == {"go_test"}
    assert mt.extract_dimension_values("backend_framework", "Backend uses Spring Boot.") == {"spring_boot"}


def test_extract_ambiguous_returns_multiple():
    values = mt.extract_dimension_values("db_engine", "Migrating from MySQL to PostgreSQL.")
    assert values == {"mysql", "postgresql"}


def test_extract_unsupported_dimension_is_empty():
    assert mt.extract_dimension_values("project_structure", "src/ holds everything") == set()


# --- Non-DB reality check (pure comparison) --------------------------------

def test_reality_match():
    result = mt.check_fact_against_signal("backend_framework", "Backend uses FastAPI.", "fastapi")
    assert result.status == mt.REALITY_MATCH
    assert result.memory_value == "fastapi"
    assert result.repo_value == "fastapi"


def test_reality_mismatch():
    result = mt.check_fact_against_signal("backend_framework", "Backend uses Flask.", "fastapi")
    assert result.status == mt.REALITY_MISMATCH
    assert result.memory_value == "flask"
    assert result.repo_value == "fastapi"


def test_reality_accepts_short_repo_token():
    # repo signal given as a short token ("postgres") still canonicalizes.
    result = mt.check_fact_against_signal("db_engine", "We use PostgreSQL.", "postgres")
    assert result.status == mt.REALITY_MATCH
    assert result.repo_value == "postgresql"


def test_reality_unknown_when_no_repo_signal():
    result = mt.check_fact_against_signal("test_runner", "Tests use pytest.", None)
    assert result.status == mt.REALITY_UNKNOWN
    assert result.memory_value == "pytest"
    assert result.repo_value is None


def test_reality_unknown_when_fact_has_no_value():
    result = mt.check_fact_against_signal("test_runner", "We care about quality.", "pytest")
    assert result.status == mt.REALITY_UNKNOWN
    assert result.memory_value is None


def test_reality_unknown_when_fact_is_ambiguous():
    result = mt.check_fact_against_signal("db_engine", "We use MySQL and MongoDB.", "mysql")
    assert result.status == mt.REALITY_UNKNOWN
    assert result.memory_value is None


def test_reality_unsupported_dimension():
    result = mt.check_fact_against_signal("project_structure", "src/ layout", "anything")
    assert result.status == mt.REALITY_UNSUPPORTED


# --- Supersession / contradiction candidates -------------------------------

def test_supersession_candidate_for_conflicting_values():
    items = [
        _item("old", "Tests use jest."),
        _item("new", "Tests use vitest."),
    ]
    candidates = mt.find_supersession_candidates(items)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.dimension == "test_runner"
    assert {cand.a_value, cand.b_value} == {"jest", "vitest"}
    assert cand.relation == mt.POSSIBLE_SUPERSESSION
    # Invariant: recency must never imply truth.
    assert cand.recency_implies_truth is False


def test_same_value_is_not_a_supersession_candidate():
    items = [
        _item("a", "Tests use pytest."),
        _item("b", "Run tests with pytest."),
    ]
    assert mt.find_supersession_candidates(items) == []


def test_unrelated_dimensions_are_not_supersession_candidates():
    items = [
        _item("a", "Backend uses FastAPI."),
        _item("b", "Frontend uses React."),
    ]
    assert mt.find_supersession_candidates(items) == []


def test_db_contradiction_is_supersession_candidate():
    items = [
        _item("a", "Project uses PostgreSQL."),
        _item("b", "Project uses MySQL."),
    ]
    candidates = mt.find_supersession_candidates(items)
    assert len(candidates) == 1
    assert candidates[0].dimension == "db_engine"
    assert {candidates[0].a_value, candidates[0].b_value} == {"postgresql", "mysql"}


# --- Aggregate + purity -----------------------------------------------------

def test_find_trust_candidates_runs_both_detectors():
    items = [
        _item("a", "Tests use pytest."),
        _item("b", "Run tests with pytest."),  # near-duplicate of a
        _item("c", "Tests use jest."),          # contradicts a/b
    ]
    result = mt.find_trust_candidates(items)
    assert any(frozenset((d.left_ref, d.right_ref)) == frozenset(("a", "b"))
               for d in result.duplicates)
    assert any(s.dimension == "test_runner" for s in result.supersessions)


def test_helpers_do_not_mutate_input():
    items = [
        _item("a", "Tests use pytest."),
        _item("b", "Tests use jest."),
    ]
    snapshot = [dict(item) for item in items]
    mt.find_duplicate_candidates(items)
    mt.find_supersession_candidates(items)
    mt.find_trust_candidates(items)
    mt.check_fact_against_signal("test_runner", "Tests use pytest.", "pytest")
    assert items == snapshot


def test_module_has_no_db_or_runtime_imports():
    # The trust helpers must stay pure: no DB engine, SQLAlchemy, FastAPI,
    # orchestrator, provider, or even backend.* imports. Parse the actual import
    # statements (not arbitrary substrings) so vocab values like "fastapi" don't
    # trip the guard.
    import ast

    tree = ast.parse(Path(mt.__file__).read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    forbidden = {"sqlalchemy", "fastapi", "subprocess", "os", "backend"}
    assert not (imported_roots & forbidden), (
        f"unexpected dependency in memory_trust: {sorted(imported_roots & forbidden)}"
    )
    # Positive check: only the expected stdlib modules are imported.
    assert imported_roots <= {"re", "dataclasses", "itertools", "typing", "__future__"}
