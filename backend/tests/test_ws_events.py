"""
Tests for Phase 2C-3 run event WebSocket stream.
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import text

from backend.db.database import engine, init_db
from backend.events.event_bus import (
    clear_all_events_for_tests,
    get_subscriber_count_for_tests,
    publish,
)
from backend.events.schema import Event
from backend.main import app
from backend.routes import ws_events

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_events():
    clear_all_events_for_tests()
    yield
    clear_all_events_for_tests()


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def run_id():
    init_db()
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pipeline_runs
            (id, project_id, feature_description, status, current_step)
            VALUES (:id, :project_id, :feature_description, 'running', 'test')
        """), {
            "id": run_id,
            "project_id": "proj-test",
            "feature_description": "Stream events",
        })
    yield run_id
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM approval_gates WHERE run_id = :run_id
        """), {"run_id": run_id})
        conn.execute(text("""
            DELETE FROM chunks WHERE run_id = :run_id
        """), {"run_id": run_id})
        conn.execute(text("""
            DELETE FROM pipeline_runs WHERE id = :run_id
        """), {"run_id": run_id})


def make_event(run_id: str, message: str = "event", kind: str = "log") -> Event:
    return Event(
        run_id=run_id,
        kind=kind,
        stage="system",
        message=message,
    )


def receive_replay_complete(websocket):
    while True:
        message = websocket.receive_json()
        if message["type"] == "replay_complete":
            return message


def wait_for_subscriber_count(run_id: str, expected: int, timeout: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_subscriber_count_for_tests(run_id) == expected:
            return
        time.sleep(0.01)
    assert get_subscriber_count_for_tests(run_id) == expected


def test_websocket_rejects_unknown_run_id(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/runs/missing/events"):
            pass


def test_websocket_replays_buffered_events(client, run_id):
    first = make_event(run_id, "first")
    second = make_event(run_id, "second")
    publish(first)
    publish(second)

    with client.websocket_connect(f"/ws/runs/{run_id}/events") as websocket:
        first_msg = websocket.receive_json()
        second_msg = websocket.receive_json()
        replay_done = websocket.receive_json()

    assert first_msg["type"] == "event"
    assert first_msg["event"]["id"] == first.id
    assert second_msg["event"]["id"] == second.id
    assert replay_done == {
        "type": "replay_complete",
        "last_event_id": second.id,
    }


def test_websocket_since_parameter_skips_seen_events(client, run_id):
    first = make_event(run_id, "first")
    second = make_event(run_id, "second")
    third = make_event(run_id, "third")
    publish(first)
    publish(second)
    publish(third)

    with client.websocket_connect(
        f"/ws/runs/{run_id}/events?since={first.id}"
    ) as websocket:
        second_msg = websocket.receive_json()
        third_msg = websocket.receive_json()
        replay_done = websocket.receive_json()

    assert second_msg["event"]["id"] == second.id
    assert third_msg["event"]["id"] == third.id
    assert replay_done["last_event_id"] == third.id


def test_websocket_unknown_since_returns_full_buffer(client, run_id):
    first = make_event(run_id, "first")
    second = make_event(run_id, "second")
    publish(first)
    publish(second)

    with client.websocket_connect(
        f"/ws/runs/{run_id}/events?since=missing"
    ) as websocket:
        first_msg = websocket.receive_json()
        second_msg = websocket.receive_json()
        replay_done = websocket.receive_json()

    assert first_msg["event"]["id"] == first.id
    assert second_msg["event"]["id"] == second.id
    assert replay_done["last_event_id"] == second.id


def test_websocket_sends_live_event_after_replay(client, run_id):
    with client.websocket_connect(f"/ws/runs/{run_id}/events") as websocket:
        replay_done = websocket.receive_json()
        assert replay_done["type"] == "replay_complete"
        wait_for_subscriber_count(run_id, 1)

        event = make_event(run_id, "live")
        publish(event)
        live_msg = websocket.receive_json()

    assert live_msg["type"] == "event"
    assert live_msg["event"]["id"] == event.id


def test_websocket_heartbeat_on_idle(monkeypatch, client, run_id):
    monkeypatch.setattr(ws_events, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

    with client.websocket_connect(f"/ws/runs/{run_id}/events") as websocket:
        assert websocket.receive_json()["type"] == "replay_complete"
        heartbeat = websocket.receive_json()

    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["ts"]


def test_websocket_closes_after_terminal_event(monkeypatch, client, run_id):
    monkeypatch.setattr(ws_events, "TERMINAL_GRACE_SECONDS", 0.01)

    with client.websocket_connect(f"/ws/runs/{run_id}/events") as websocket:
        assert websocket.receive_json()["type"] == "replay_complete"
        wait_for_subscriber_count(run_id, 1)

        event = make_event(run_id, "terminal", kind="terminal")
        publish(event)
        event_msg = websocket.receive_json()
        close_msg = websocket.receive_json()

    assert event_msg["type"] == "event"
    assert event_msg["event"]["id"] == event.id
    assert close_msg == {
        "type": "close",
        "reason": "run_terminal",
    }


def test_bad_origin_rejected(client, run_id):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws/runs/{run_id}/events",
            headers={"Origin": "http://evil.example"},
        ):
            pass


def test_allowed_origin_accepted(client, run_id):
    with client.websocket_connect(
        f"/ws/runs/{run_id}/events",
        headers={"Origin": "http://localhost:5173"},
    ) as websocket:
        replay_done = websocket.receive_json()

    assert replay_done["type"] == "replay_complete"


def test_disconnect_unregisters_subscriber(client, run_id):
    with client.websocket_connect(f"/ws/runs/{run_id}/events") as websocket:
        assert websocket.receive_json()["type"] == "replay_complete"
        wait_for_subscriber_count(run_id, 1)

    wait_for_subscriber_count(run_id, 0)
