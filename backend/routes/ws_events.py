"""
ws_events.py
WebSocket route for streaming buffered and live run events.
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import text

from backend.db.database import engine
from backend.events import event_bus
from backend.events.schema import Event

router = APIRouter()

HEARTBEAT_INTERVAL_SECONDS = 15
TERMINAL_GRACE_SECONDS = 1

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_to_dict(event: Event) -> dict:
    if hasattr(event, "model_dump"):
        return event.model_dump()
    return event.dict()


def _run_exists(run_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT 1 FROM pipeline_runs
            WHERE id = :run_id
            LIMIT 1
        """), {"run_id": run_id}).fetchone()
    return row is not None


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    return origin is None or origin in ALLOWED_ORIGINS


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:
        pass


def _is_terminal_event(event: Event) -> bool:
    return event.kind == "terminal"


async def _send_event(websocket: WebSocket, event: Event) -> None:
    await websocket.send_json({
        "type": "event",
        "event": _event_to_dict(event),
    })


@router.websocket("/ws/runs/{run_id}/events")
async def run_events_websocket(websocket: WebSocket, run_id: str):
    since = websocket.query_params.get("since")

    if not _origin_allowed(websocket):
        await _safe_close(websocket, 1008)
        return

    try:
        if not _run_exists(run_id):
            await _safe_close(websocket, 1008)
            return

        await websocket.accept()

        replayed = event_bus.get_buffered_events(run_id, since)
        last_event_id = since
        for event in replayed:
            await _send_event(websocket, event)
            last_event_id = event.id

        await websocket.send_json({
            "type": "replay_complete",
            "last_event_id": last_event_id,
        })

        async with event_bus.subscription(run_id) as queue:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await websocket.send_json({
                        "type": "heartbeat",
                        "ts": _utc_iso(),
                    })
                    continue

                await _send_event(websocket, event)
                if _is_terminal_event(event):
                    await asyncio.sleep(TERMINAL_GRACE_SECONDS)
                    await websocket.send_json({
                        "type": "close",
                        "reason": "run_terminal",
                    })
                    await websocket.close(code=1000)
                    return
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[WS] run event stream failed: {error}")
        await _safe_close(websocket, 1011)
