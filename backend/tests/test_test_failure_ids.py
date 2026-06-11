import pytest

from backend.pipeline.test_failure_ids import extract_pytest_failing_test_ids

pytestmark = pytest.mark.unit


def test_extracts_pytest_failed_node_ids():
    result = extract_pytest_failing_test_ids(
        "================ short test summary info ================\n"
        "FAILED tests/test_app.py::test_old - AssertionError\n"
        "FAILED tests/test_app.py::TestApi::test_new[param value] - assert 1 == 2\n"
        "2 failed, 4 passed in 0.30s\n"
    )

    assert result.parseable is True
    assert result.failed_ids == frozenset({
        "tests/test_app.py::test_old",
        "tests/test_app.py::TestApi::test_new[param value]",
    })
    assert result.expected_failed_count == 2


def test_failed_count_without_node_ids_is_not_parseable():
    result = extract_pytest_failing_test_ids("1 failed, 4 passed in 0.30s")

    assert result.parseable is False
    assert result.failed_ids == frozenset()


def test_partial_failed_node_id_parse_is_not_parseable():
    result = extract_pytest_failing_test_ids(
        "FAILED tests/test_app.py::test_old - AssertionError\n"
        "2 failed, 4 passed in 0.30s\n"
    )

    assert result.parseable is False
    assert result.failed_ids == frozenset({"tests/test_app.py::test_old"})


def test_no_failure_evidence_is_parseable_empty():
    result = extract_pytest_failing_test_ids("6 passed in 0.30s")

    assert result.parseable is True
    assert result.failed_ids == frozenset()
