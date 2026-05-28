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
    "is there any issue in the code",
    "any bugs in the codebase",
    "issues in code",
    "any problems with the auth flow",
    "what is wrong with the parser",
    "what's wrong with the parser",
    "summarize the auth flow without changing code",
    "just explain how triage works",
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
    "fix auth bug",
    "use the best design and implement login",
    "design and implement login",
])
def test_implementation_intents(text):
    assert classify_intent(text) == "implementation"


@pytest.mark.parametrize("text", [
    "give me the best design for login",
    "give me the best design and don't implement",
])
def test_soft_plan_phrases_classify_as_plan_only(text):
    assert classify_intent(text) == "plan_only"


def test_explain_with_design_and_read_only_safety_is_not_implementation():
    assert classify_intent(
        "explain the best design, don't change code"
    ) in {"plan_only", "report_only"}


def test_ambiguous_request_defaults_to_plan_only():
    assert classify_intent("authentication thoughts") == "plan_only"
