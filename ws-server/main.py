import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import redis.asyncio as redis

app = FastAPI(title="Section Control WS Bridge")

# In compose, the browser origin is localhost:3000 but this container talks to
# the "redis" service by name. Both must be configurable or the same code cannot
# run on a laptop and in compose.
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

TELEMETRY_STREAM = "telemetry_stream"
DECISION_STREAM = "decision_stream"
ACTION_STREAM = "action_stream"

# How much history a newly connected dashboard gets. Without this, a browser
# refresh shows an empty panel until every train re-reports -- up to 3s of
# blankness, which is exactly when a judge is looking at the screen.
BACKFILL_COUNT = 200

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


@app.get("/healthz")
async def healthz():
    try:
        await redis_client.ping()
        return {"status": "ok", "redis": f"{REDIS_HOST}:{REDIS_PORT}"}
    except Exception as exc:
        return {"status": "degraded", "error": str(exc)}


async def _backfill(websocket: WebSocket, stream: str) -> str:
    """Replay recent entries so a fresh client has a populated panel at once.

    Returns the last id seen, so the live loop picks up exactly where this left
    off with no gap and no duplicates.
    """
    entries = await redis_client.xrevrange(stream, count=BACKFILL_COUNT)
    last_id = "0-0"
    for message_id, fields in reversed(entries):
        payload = fields.get("payload")
        if payload:
            await websocket.send_text(payload)
        last_id = message_id
    return last_id if entries else "$"


async def _pump(websocket: WebSocket) -> None:
    """Redis -> browser. Reads both streams so telemetry and decisions arrive
    interleaved on one socket, which is what the client's event_type switch
    already expects."""
    cursors = {
        TELEMETRY_STREAM: await _backfill(websocket, TELEMETRY_STREAM),
        DECISION_STREAM: await _backfill(websocket, DECISION_STREAM), # Fixed
    }

    while True:
        messages = await redis_client.xread(cursors, count=100, block=1000)
        for stream_name, stream_messages in messages:
            for message_id, fields in stream_messages:
                cursors[stream_name] = message_id
                payload = fields.get("payload")
                if payload:
                    await websocket.send_text(payload)
        await asyncio.sleep(0)


async def _listen(websocket: WebSocket) -> None:
    """Browser -> Redis. Contract 5: the controller's chosen scenario goes onto
    its own stream for the simulator to consume and act on."""
    while True:
        raw = await websocket.receive_text()
        try:
            action = json.loads(raw)
        except json.JSONDecodeError:
            print(f"Discarded malformed action frame: {raw[:120]}")
            continue

        if action.get("event_type") != "CONTROLLER_ACTION":
            print(f"Discarded unexpected upstream event: {action.get('event_type')}")
            continue

        await redis_client.xadd(ACTION_STREAM, {"payload": json.dumps(action)})
        print(
            f"Controller committed {action.get('scenario_id')} "
            f"for {action.get('conflict_id')}"
        )


@app.websocket("/ws/telemetry")
async def telemetry_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Run both directions concurrently; whichever finishes first (usually a
    # disconnect) tears the other down.
    pump = asyncio.create_task(_pump(websocket))
    listen = asyncio.create_task(_listen(websocket))

    try:
        done, pending = await asyncio.wait(
            {pump, listen}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # surface a real error rather than swallowing it
    except WebSocketDisconnect:
        print("Dashboard client disconnected.")
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"WebSocket error: {exc}")
    finally:
        for task in (pump, listen):
            if not task.done():
                task.cancel()
