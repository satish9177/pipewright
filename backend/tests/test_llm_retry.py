"""
test_llm_retry.py
Unit tests for the shared async rate-limit retry executor (E4).

No live provider calls and no real waiting: the operation is a counting fake
and asyncio.sleep is monkeypatched to record requested delays. Also pins the
deletion of the old 60-second lock-held sleeps in planner.py / coder.py by
asserting on their source text.
"""

from pathlib import Path

import pytest

from backend.llm import retry as llm_retry
from backend.llm.errors import ProviderRateLimitError
from backend.llm.retry import (
    RATE_LIMIT_MAX_ATTEMPTS,
    ProviderRetryExhaustedError,
    call_with_rate_limit_retry,
)
from backend.pipeline import coder, planner

pytestmark = pytest.mark.unit


def _rate_limit_error(retry_after_seconds=None) -> ProviderRateLimitError:
    return ProviderRateLimitError(
        "rate limited",
        provider="fake",
        model="fake-model",
        retry_after_seconds=retry_after_seconds,
    )


def _patch_sleep(monkeypatch) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)
    return sleeps


def _no_jitter(monkeypatch) -> None:
    monkeypatch.setattr(llm_retry.random, "uniform", lambda a, b: 0.0)


class _Operation:
    """Awaitable fake that raises the queued errors, then returns a value."""

    def __init__(self, errors, result="ok"):
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


@pytest.mark.asyncio
async def test_success_on_first_attempt_no_sleep(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    op = _Operation([])

    result = await call_with_rate_limit_retry(op)

    assert result == "ok"
    assert op.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_rate_limit_retried_until_success(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    _no_jitter(monkeypatch)
    op = _Operation([_rate_limit_error(), _rate_limit_error()])

    result = await call_with_rate_limit_retry(op)

    assert result == "ok"
    assert op.calls == 3
    # Exponential backoff: base 2s, then 4s.
    assert sleeps == [2.0, 4.0]


@pytest.mark.asyncio
async def test_exhaustion_raises_typed_error_after_max_attempts(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    op = _Operation([_rate_limit_error()] * 10)

    with pytest.raises(ProviderRetryExhaustedError) as error:
        await call_with_rate_limit_retry(op)

    assert op.calls == RATE_LIMIT_MAX_ATTEMPTS
    # Sleeps happen between attempts only, never after the last one.
    assert len(sleeps) == RATE_LIMIT_MAX_ATTEMPTS - 1
    assert isinstance(error.value.__cause__, ProviderRateLimitError)
    assert error.value.provider == "fake"


@pytest.mark.asyncio
async def test_retry_after_header_is_honored(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    _no_jitter(monkeypatch)
    # Retry-After larger than the 2s backoff for the first retry.
    op = _Operation([_rate_limit_error(retry_after_seconds=7)])

    result = await call_with_rate_limit_retry(op)

    assert result == "ok"
    assert len(sleeps) == 1
    assert sleeps[0] >= 7.0


@pytest.mark.asyncio
async def test_429_string_fallback_is_treated_as_rate_limit(monkeypatch):
    # Parity with the previous planner/coder detection: a plain exception whose
    # text contains "429" is retried.
    sleeps = _patch_sleep(monkeypatch)
    op = _Operation([RuntimeError("429 rate limit")])

    result = await call_with_rate_limit_retry(op)

    assert result == "ok"
    assert op.calls == 2
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_non_rate_limit_error_propagates_immediately(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    op = _Operation([ValueError("bad json")])

    with pytest.raises(ValueError):
        await call_with_rate_limit_retry(op)

    assert op.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_exhaustion_message_is_descriptive(monkeypatch):
    _patch_sleep(monkeypatch)
    op = _Operation([_rate_limit_error()] * 10)

    with pytest.raises(ProviderRetryExhaustedError) as error:
        await call_with_rate_limit_retry(op, description="planner call")

    assert "planner call" in str(error.value)
    assert str(RATE_LIMIT_MAX_ATTEMPTS) in str(error.value)


# ---------------------------------------------------------------------------
# The old 60-second lock-held sleeps are gone.
# ---------------------------------------------------------------------------


def _source_of(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def test_planner_and_coder_no_longer_sleep_sixty_seconds():
    assert "asyncio.sleep(60)" not in _source_of(planner)
    assert "asyncio.sleep(60)" not in _source_of(coder)
