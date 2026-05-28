import json

import pytest

from backend.llm.base import LLMResponse
from backend.pipeline import intent as intent_module
from backend.pipeline.intent import (
    LLM_MAX_DESCRIPTION_CHARS,
    classify_intent,
    classify_intent_async,
)

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
    "add a health endpoint",
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


# --------------------------------------------------------------------------
# Long-description: read-only blocker must scan the FULL text, not just the
# first 300 chars sent to the LLM.
# --------------------------------------------------------------------------

def _padded_request(prefix: str, suffix: str, total: int = 600) -> str:
    filler = " ambiguous prose " * 40
    raw = prefix + filler + suffix
    if len(raw) < total:
        raw = raw + (" filler" * ((total - len(raw)) // 7))
    return raw


def test_long_description_with_trailing_read_only_blocker_not_implementation():
    text = _padded_request("please help me with this. ", " also, don't change code.")
    assert len(text) > LLM_MAX_DESCRIPTION_CHARS

    result = classify_intent(text)

    assert result != "implementation"


# --------------------------------------------------------------------------
# LLM fallback: only triggers when no deterministic layer matched. Use a
# spy on backend.pipeline.intent.complete_for_role to control behavior.
# --------------------------------------------------------------------------

class _SpyLLM:
    def __init__(self, response_text: str | Exception):
        self.response_text = response_text
        self.calls: list[dict] = []

    async def __call__(self, role, request, overrides=None):
        self.calls.append({
            "role": role,
            "request": request,
            "user_content": request.messages[-1].content,
        })
        if isinstance(self.response_text, Exception):
            raise self.response_text
        return LLMResponse(
            text=self.response_text,
            provider="fake",
            model="fake-model",
            input_tokens=10,
            output_tokens=10,
            finish_reason="stop",
        )


def _install_llm_spy(monkeypatch, response_text: str | Exception) -> _SpyLLM:
    spy = _SpyLLM(response_text)
    monkeypatch.setattr(intent_module, "complete_for_role", spy)
    return spy


@pytest.mark.asyncio
async def test_llm_fallback_implementation_high_confidence(monkeypatch):
    spy = _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.92, "reason": "ship it"}),
    )

    result = await classify_intent_async("make this production ready")

    assert result == "implementation"
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_llm_fallback_implementation_just_above_threshold(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.88, "reason": "ok"}),
    )

    result = await classify_intent_async("handle login end to end")

    assert result == "implementation"


@pytest.mark.asyncio
async def test_llm_fallback_implementation_low_confidence_downgrades(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.65, "reason": "maybe"}),
    )

    result = await classify_intent_async("take care of this")

    assert result == "plan_only"


@pytest.mark.asyncio
async def test_llm_fallback_implementation_very_low_confidence_downgrades(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.55, "reason": "uncertain"}),
    )

    result = await classify_intent_async("can you do something about auth")

    assert result == "plan_only"


@pytest.mark.asyncio
async def test_llm_fallback_plan_only_accepted_at_low_confidence(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "plan_only", "confidence": 0.70, "reason": "plan"}),
    )

    result = await classify_intent_async("make this production ready")

    assert result == "plan_only"


@pytest.mark.asyncio
async def test_llm_fallback_report_only_accepted_at_low_confidence(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "report_only", "confidence": 0.4, "reason": "info"}),
    )

    result = await classify_intent_async("take care of this")

    assert result == "report_only"


@pytest.mark.asyncio
async def test_llm_fallback_provider_error_defaults_to_plan_only(monkeypatch):
    _install_llm_spy(monkeypatch, RuntimeError("network down"))

    result = await classify_intent_async("make this production ready")

    assert result == "plan_only"


@pytest.mark.asyncio
async def test_llm_fallback_invalid_json_defaults_to_plan_only(monkeypatch):
    _install_llm_spy(monkeypatch, "not json at all")

    result = await classify_intent_async("make this production ready")

    assert result == "plan_only"


@pytest.mark.asyncio
async def test_llm_fallback_unrecognized_intent_defaults_to_plan_only(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "go_for_it", "confidence": 0.99, "reason": "n/a"}),
    )

    result = await classify_intent_async("make this production ready")

    assert result == "plan_only"


@pytest.mark.asyncio
async def test_llm_fallback_missing_confidence_defaults_to_plan_only(monkeypatch):
    _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "reason": "missing field"}),
    )

    result = await classify_intent_async("make this production ready")

    assert result == "plan_only"


@pytest.mark.parametrize("confidence", [1.5, -0.1, "high"])
@pytest.mark.asyncio
async def test_llm_fallback_invalid_confidence_defaults_to_plan_only(
    monkeypatch, confidence
):
    _install_llm_spy(
        monkeypatch,
        json.dumps({
            "intent": "implementation",
            "confidence": confidence,
            "reason": "bad",
        }),
    )

    result = await classify_intent_async("make this production ready")

    assert result == "plan_only"


# --------------------------------------------------------------------------
# LLM skipped: deterministic results must not call the LLM at all.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("add login feature", "implementation"),
    ("is there any issue in the code", "report_only"),
    ("give me a plan to add login", "plan_only"),
])
@pytest.mark.asyncio
async def test_deterministic_results_do_not_call_llm(monkeypatch, text, expected):
    spy = _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.99, "reason": "x"}),
    )

    result = await classify_intent_async(text)

    assert result == expected
    assert spy.calls == []


@pytest.mark.asyncio
async def test_read_only_blocker_overrides_implementation_verb_and_skips_llm(
    monkeypatch,
):
    spy = _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.99, "reason": "x"}),
    )

    result = await classify_intent_async("don't change code and just implement it")

    assert result != "implementation"
    assert spy.calls == []


# --------------------------------------------------------------------------
# Long-description: LLM receives only the first 300 characters.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_input_is_truncated_to_first_300_chars(monkeypatch):
    spy = _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.95, "reason": "ok"}),
    )
    head = "wibble " * 60  # ~420 chars of neutral filler
    sentinel = "ZZ_BEYOND_CUTOFF_SENTINEL_ZZ"
    ambiguous_long = head + sentinel
    assert len(head) > LLM_MAX_DESCRIPTION_CHARS

    result = await classify_intent_async(ambiguous_long)

    assert result == "implementation"
    assert len(spy.calls) == 1
    user_content = spy.calls[0]["user_content"]
    truncated_chunk = ambiguous_long[:LLM_MAX_DESCRIPTION_CHARS]
    assert truncated_chunk in user_content
    assert sentinel not in user_content


@pytest.mark.asyncio
async def test_long_description_with_trailing_blocker_skips_llm(monkeypatch):
    spy = _install_llm_spy(
        monkeypatch,
        json.dumps({"intent": "implementation", "confidence": 0.99, "reason": "x"}),
    )
    text = _padded_request("please help me with this. ", " also, don't change code.")
    assert len(text) > LLM_MAX_DESCRIPTION_CHARS

    result = await classify_intent_async(text)

    assert result != "implementation"
    assert spy.calls == []
