import pytest

from backend.llm.sanitize import sanitize_for_log

pytestmark = pytest.mark.unit


def test_sanitize_redacts_provider_keys_and_bearer_tokens():
    text = (
        "gemini AIzaSyA123456789012345678901234567890 "
        "openai sk-abcdefghijklmnopqrstuvwxyz123456 "
        "anthropic sk-ant-abcdefghijklmnopqrstuvwxyz123456 "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    )

    sanitized = sanitize_for_log(text)

    assert "AIzaSyA" not in sanitized
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert "abcdefghijklmnopqrstuvwxyz123456" not in sanitized
    assert sanitized.count("[REDACTED]") >= 4


def test_sanitize_redacts_secret_context_tokens():
    raw = "secret=abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
    sanitized = sanitize_for_log(raw)

    assert "abcdefghijklmnopqrstuvwxyz1234567890ABCDEF" not in sanitized
    assert "secret=[REDACTED]" in sanitized


def test_sanitize_keeps_plain_commit_hash():
    commit = "0123456789abcdef0123456789abcdef01234567"
    text = f"commit {commit} failed tests"

    assert sanitize_for_log(text) == text


def test_sanitize_redacts_commit_like_value_near_secret_context():
    commit_like = "0123456789abcdef0123456789abcdef01234567"
    sanitized = sanitize_for_log(f"token: {commit_like}")

    assert commit_like not in sanitized
    assert "[REDACTED]" in sanitized
