from types import SimpleNamespace

import pytest

from backend.llm.base import LLMRequest, Message
from backend.llm.providers import anthropic, deepseek, gemini, openai
from backend.llm.providers.fake import FakeProvider
from backend.pipeline import coder, planner, policy

pytestmark = pytest.mark.unit


def _request(messages: list[Message]) -> LLMRequest:
    return LLMRequest(messages=messages, model="test-model")


def _response_with_usage(usage: SimpleNamespace):
    block = SimpleNamespace(type="text", text="ok")
    return SimpleNamespace(content=[block], usage=usage, stop_reason="end_turn")


def test_prompt_cache_policy_defaults_disabled():
    assert policy.PROMPT_CACHE_ENABLED is False
    assert policy.GEMINI_EXPLICIT_CACHE_ENABLED is False


def test_anthropic_cache_disabled_marked_system_emits_legacy_string(monkeypatch):
    monkeypatch.setattr(policy, "PROMPT_CACHE_ENABLED", False)
    req = _request([
        Message(role="system", content="Stable one.", cache=True),
        Message(role="system", content="Stable two."),
        Message(role="user", content="ping"),
    ])

    system, messages = anthropic._translate_messages(req)

    assert system == "Stable one.\n\nStable two."
    assert messages == [{"role": "user", "content": "ping"}]


def test_anthropic_cache_enabled_unmarked_system_emits_legacy_string(monkeypatch):
    monkeypatch.setattr(policy, "PROMPT_CACHE_ENABLED", True)
    req = _request([
        Message(role="system", content="Stable one."),
        Message(role="system", content="Stable two."),
        Message(role="user", content="ping"),
    ])

    system, messages = anthropic._translate_messages(req)

    assert system == "Stable one.\n\nStable two."
    assert messages == [{"role": "user", "content": "ping"}]


def test_anthropic_cache_enabled_marks_only_marked_system_text(monkeypatch):
    monkeypatch.setattr(policy, "PROMPT_CACHE_ENABLED", True)
    req = _request([
        Message(role="system", content="Static system.", cache=True),
        Message(role="system", content="Unmarked system."),
        Message(role="user", content="Request-varying user prompt.", cache=True),
    ])

    system, messages = anthropic._translate_messages(req)

    assert system == [
        {
            "type": "text",
            "text": "Static system.",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n\n"},
        {"type": "text", "text": "Unmarked system."},
    ]
    assert "".join(block["text"] for block in system) == (
        "Static system.\n\nUnmarked system."
    )
    assert messages == [{"role": "user", "content": "Request-varying user prompt."}]


def test_planner_and_coder_mark_only_static_system_messages():
    requests = [
        planner._build_llm_request("FEATURE REQUEST:\nUse memory"),
        planner._build_correction_request("FEATURE REQUEST:\nUse memory", "bad", ValueError("x")),
        coder._build_llm_request("IMPLEMENTATION PLAN:\nUse memory and files"),
        coder._build_correction_request(
            "IMPLEMENTATION PLAN:\nUse memory and files",
            "bad",
            ValueError("x"),
        ),
    ]

    for request in requests:
        assert request.messages[0].role == "system"
        assert request.messages[0].cache is True
        assert all(message.cache is False for message in request.messages[1:])


def test_non_anthropic_providers_ignore_cache_marker(monkeypatch):
    monkeypatch.setattr(policy, "PROMPT_CACHE_ENABLED", True)
    req = _request([
        Message(role="system", content="Static system.", cache=True),
        Message(role="assistant", content="Previous answer.", cache=True),
        Message(role="user", content="Request prompt.", cache=True),
    ])

    assert openai._translate_messages(req) == [
        {"role": "system", "content": "Static system."},
        {"role": "assistant", "content": "Previous answer."},
        {"role": "user", "content": "Request prompt."},
    ]
    assert deepseek._translate_messages(req) == [
        {"role": "system", "content": "Static system."},
        {"role": "assistant", "content": "Previous answer."},
        {"role": "user", "content": "Request prompt."},
    ]

    system_instruction, contents = gemini._translate_messages(req)

    assert system_instruction == "Static system."
    assert contents == [
        {"role": "model", "parts": [{"text": "Previous answer."}]},
        {"role": "user", "parts": [{"text": "Request prompt."}]},
    ]


@pytest.mark.asyncio
async def test_fake_provider_ignores_cache_marker():
    provider = FakeProvider()
    unmarked = await provider.complete(
        LLMRequest(
            messages=[Message(role="user", content="hello")],
            model="fake-model",
        )
    )
    marked = await provider.complete(
        LLMRequest(
            messages=[Message(role="user", content="hello", cache=True)],
            model="fake-model",
        )
    )

    assert marked == unmarked


def test_anthropic_cache_usage_fields_are_behavior_neutral():
    cache_hit_usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=99,
    )
    cache_miss_usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=99,
    )

    cache_hit = anthropic._to_llm_response(
        _response_with_usage(cache_hit_usage),
        "anthropic",
        "claude-test",
    )
    cache_miss = anthropic._to_llm_response(
        _response_with_usage(cache_miss_usage),
        "anthropic",
        "claude-test",
    )

    assert cache_hit == cache_miss
    assert "cache" not in str(cache_hit.raw).lower()
