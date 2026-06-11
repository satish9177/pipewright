"""
Shared async retry executor for provider calls (E4).

One boring helper owns rate-limit retry policy for every stage that calls a
provider: exponential backoff with jitter, ``Retry-After`` honored when the
provider supplied it, a small bounded attempt budget, and a typed exhaustion
error the caller converts into its own failure report.

This replaces the per-stage ``asyncio.sleep(60)`` + single-retry blocks in
``planner.py`` / ``coder.py``. Those sleeps ran while the project repo lock was
held, stalling every other run on the same project for a full minute; the
bounded backoff here keeps a worst-case wait to seconds.

Only rate-limit errors are retried. Anything else propagates immediately and
unchanged — parse-retry and other recovery logic stay with the callers.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from backend.llm.errors import LLMError, ProviderRateLimitError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Total attempts including the first call (policy default: 3).
RATE_LIMIT_MAX_ATTEMPTS = 3
# Backoff schedule: base * 2**retry_index, capped, plus uniform jitter.
RATE_LIMIT_BASE_DELAY_SECONDS = 2.0
RATE_LIMIT_MAX_DELAY_SECONDS = 30.0
RATE_LIMIT_JITTER_SECONDS = 1.0


class ProviderRetryExhaustedError(LLMError):
    """All bounded rate-limit retries failed; the last provider error is chained."""


def _is_rate_limit(error: Exception) -> bool:
    # The structured error is authoritative; the "429" substring fallback
    # preserves the previous planner/coder detection for providers that raise
    # plain exceptions.
    return isinstance(error, ProviderRateLimitError) or "429" in str(error)


def _retry_delay_seconds(error: Exception, retry_index: int) -> float:
    """Backoff for the given retry (0-based), honoring Retry-After when present."""
    delay = min(
        RATE_LIMIT_MAX_DELAY_SECONDS,
        RATE_LIMIT_BASE_DELAY_SECONDS * (2 ** retry_index),
    )
    retry_after = getattr(error, "retry_after_seconds", None)
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay + random.uniform(0, RATE_LIMIT_JITTER_SECONDS)


async def call_with_rate_limit_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = RATE_LIMIT_MAX_ATTEMPTS,
    description: str = "provider call",
) -> T:
    """
    Await ``operation()`` with bounded rate-limit retry.

    Non-rate-limit errors propagate immediately on the first occurrence.
    Rate-limit errors are retried up to ``max_attempts`` total attempts with
    exponential backoff + jitter (``Retry-After`` honored when the exception
    carries ``retry_after_seconds``); exhaustion raises
    :class:`ProviderRetryExhaustedError` chaining the last provider error.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as error:
            if not _is_rate_limit(error):
                raise
            last_error = error
            if attempt >= max_attempts:
                break
            delay = _retry_delay_seconds(error, retry_index=attempt - 1)
            logger.warning(
                "[LLM-RETRY] Rate limited on %s (attempt %s/%s); "
                "retrying in %.1fs",
                description, attempt, max_attempts, delay,
            )
            await asyncio.sleep(delay)

    raise ProviderRetryExhaustedError(
        f"Rate limited on {description}; gave up after {max_attempts} attempts. "
        f"Last error: {last_error}",
        provider=getattr(last_error, "provider", "unknown"),
        model=getattr(last_error, "model", None),
        retryable=False,
    ) from last_error
