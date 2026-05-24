"""
event_bus.py
Process-local in-memory event bus for Phase 2C live observability.

Safety rule: event bus failures must never affect pipeline execution. publish()
therefore catches every internal error and returns None.
"""

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

from backend.events.schema import Event

BUFFER_LIMIT = 500
SUBSCRIBER_QUEUE_LIMIT = 200

_buffers: dict[str, deque[Event]] = defaultdict(lambda: deque(maxlen=BUFFER_LIMIT))
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def _store_event(event: Event) -> None:
    _buffers[event.run_id].append(event)


def _fanout_event(event: Event) -> None:
    subscribers = list(_subscribers.get(event.run_id, set()))
    dropped = []

    for queue in subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            dropped.append(queue)
        except Exception:
            dropped.append(queue)

    for queue in dropped:
        try:
            _subscribers[event.run_id].discard(queue)
        except Exception:
            pass


def publish(event: Event) -> None:
    """
    Store and fan out an event.

    This function must never raise and must never block pipeline execution.
    """
    try:
        _store_event(event)
        _fanout_event(event)
    except Exception as error:
        print(f"[EVENT_BUS] publish failed, ignored: {error}")


def get_buffered_events(run_id: str, since: str | None = None) -> list[Event]:
    events = list(_buffers.get(run_id, deque()))
    if since is None:
        return events

    for index, event in enumerate(events):
        if event.id == since:
            return events[index + 1:]
    return events


@asynccontextmanager
async def subscription(run_id: str) -> AsyncIterator[asyncio.Queue]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
    _subscribers[run_id].add(queue)
    try:
        yield queue
    finally:
        _subscribers[run_id].discard(queue)
        if not _subscribers[run_id]:
            _subscribers.pop(run_id, None)


def clear_all_events_for_tests() -> None:
    _buffers.clear()
    _subscribers.clear()
