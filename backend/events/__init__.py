"""
Process-local event bus foundation for live observability.

Phase 2C-1 is intentionally inert: no pipeline stages publish events yet.
"""

from backend.events.event_bus import (
    clear_all_events_for_tests,
    get_buffered_events,
    publish,
    subscription,
)
from backend.events.schema import Event

__all__ = [
    "Event",
    "clear_all_events_for_tests",
    "get_buffered_events",
    "publish",
    "subscription",
]
