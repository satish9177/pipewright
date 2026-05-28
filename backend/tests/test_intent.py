import pytest

from backend.pipeline.intent import classify_intent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("text", [
    "find bugs in the codebase",
    "review the repo",
    "audit codebase",
    "explain the project",
    "explain the project, don't change code",
    "can u explain the project, don't add any code or change any single line",
])
def test_report_only_intents(text):
    assert classify_intent(text) == "report_only"


@pytest.mark.parametrize("text", [
    "give me a plan for X",
    "give me a plan to add X",
    "design a chunk plan",
    "what would it take to add login",
])
def test_plan_only_intents(text):
    assert classify_intent(text) == "plan_only"


@pytest.mark.parametrize("text", [
    "add login feature",
    "fix the auth bug",
    "update the README",
])
def test_implementation_intents(text):
    assert classify_intent(text) == "implementation"


def test_ambiguous_request_defaults_to_plan_only():
    assert classify_intent("authentication thoughts") == "plan_only"
