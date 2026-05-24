"""
Tests for Phase 2C-1 in-memory event bus foundation.
"""

import asyncio

import pytest

from backend.events import event_bus
from backend.events.event_bus import (
    clear_all_events_for_tests,
    get_buffered_events,
    publish,
    subscription,
)
from backend.events.schema import Event

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_bus():
    clear_all_events_for_tests()
    yield
    clear_all_events_for_tests()


def make_event(run_id: str = "run-1", message: str = "hello") -> Event:
    return Event(
        run_id=run_id,
        kind="log",
        stage="system",
        message=message,
    )


def test_publish_never_raises_even_if_internal_buffering_fails(monkeypatch):
    monkeypatch.setattr(
        event_bus,
        "_store_event",
        lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    publish(make_event())


def test_publish_stores_event_in_buffer():
    event = make_event()

    publish(event)

    assert get_buffered_events("run-1") == [event]


def test_ring_buffer_caps_at_500_events():
    for index in range(501):
        publish(make_event(message=f"event {index}"))

    events = get_buffered_events("run-1")

    assert len(events) == 500
    assert events[0].message == "event 1"
    assert events[-1].message == "event 500"


def test_get_buffered_events_returns_all_events_when_since_is_none():
    first = make_event(message="first")
    second = make_event(message="second")
    publish(first)
    publish(second)

    assert get_buffered_events("run-1") == [first, second]


def test_get_buffered_events_returns_events_after_matching_since_id():
    first = make_event(message="first")
    second = make_event(message="second")
    third = make_event(message="third")
    publish(first)
    publish(second)
    publish(third)

    assert get_buffered_events("run-1", since=first.id) == [second, third]


def test_get_buffered_events_returns_full_buffer_if_since_id_not_found():
    first = make_event(message="first")
    second = make_event(message="second")
    publish(first)
    publish(second)

    assert get_buffered_events("run-1", since="missing") == [first, second]


@pytest.mark.asyncio
async def test_publish_fans_out_to_multiple_subscribers():
    event = make_event()

    async with subscription("run-1") as first:
        async with subscription("run-1") as second:
            publish(event)

            assert await first.get() == event
            assert await second.get() == event


@pytest.mark.asyncio
async def test_slow_full_subscriber_is_dropped_and_publish_does_not_block():
    async with subscription("run-1") as queue:
        for index in range(event_bus.SUBSCRIBER_QUEUE_LIMIT):
            queue.put_nowait(make_event(message=f"queued {index}"))

        publish(make_event(message="drop me"))

        assert queue not in event_bus._subscribers.get("run-1", set())


class BrokenSubscriber:
    def put_nowait(self, event):
        raise RuntimeError("subscriber failed")


@pytest.mark.asyncio
async def test_one_broken_subscriber_does_not_affect_another_subscriber():
    event = make_event()
    broken = BrokenSubscriber()

    async with subscription("run-1") as good:
        event_bus._subscribers["run-1"].add(broken)

        publish(event)

        assert await good.get() == event
        assert broken not in event_bus._subscribers["run-1"]


@pytest.mark.asyncio
async def test_subscription_unregisters_queue_on_exit():
    async with subscription("run-1") as queue:
        assert queue in event_bus._subscribers["run-1"]

    assert "run-1" not in event_bus._subscribers


def test_event_message_truncates_to_500_chars():
    event = make_event(message="x" * 600)

    assert len(event.message) == 500


def test_event_has_generated_id_and_timestamp_when_omitted():
    event = make_event()

    assert event.id
    assert event.ts
