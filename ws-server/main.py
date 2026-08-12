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
CONTROL_STREAM = "control_stream"

# Ceiling on the server-side scan, not on what is sent. Deduplication decides
# the send size; this only bounds how far back a scan will walk.
SCAN_LIMIT = 2000

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


def _snapshot_key(event: dict):
    """Identity under which an event supersedes an older one of the same kind.

    The only place in this service that knows contract field names.
    """
    kind = event.get("event_type")
    if kind in ("CONFLICT_PREDICTED", "DISPATCH_RECOMMENDATION"):
        return (kind, event.get("conflict_id"))
    if kind == "CONTROLLER_ACTION_RESULT":
        return (kind, event.get("conflict_id"), event.get("scenario_id"))
    if kind == "TRAIN_TELEMETRY":
        return (kind, event.get("train_id"))
    if kind == "SIMULATION_TICK":
        return (kind,)
    return None


async def _boot_floor():
    """Stream id of the current run's SYSTEM_READY, plus its payload.

    Scans for SYSTEM_READY specifically rather than taking the newest entry:
    control_stream also carries action verdicts, so the last entry is usually a
    result, and flooring on that would hide every conflict raised before the
    controller's most recent button press.

    Redis stream ids are monotonic and the simulator writes SYSTEM_READY after
    committing static state and before the first tick, so anything with a higher
    id belongs to the current epoch with no per-message field to trust.
    """
    entries = await redis_client.xrevrange(CONTROL_STREAM, count=SCAN_LIMIT)
    for message_id, fields in entries:
        payload = fields.get("payload")
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") == "SYSTEM_READY":
            return message_id, payload
    return None, None


async def _snapshot_since(websocket: WebSocket, stream: str, floor_id: str) -> str:
    """Send current state, not history: the newest entry per key since boot.

    Returns the cursor for the live loop -- gap-free and duplicate-free.
    """
    entries = await redis_client.xrevrange(
        stream, max="+", min=f"({floor_id}", count=SCAN_LIMIT
    )
    if not entries:
        return floor_id

    seen = set()
    latest = []
    for _, fields in entries:
        payload = fields.get("payload")
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        key = _snapshot_key(event)
        if key is None or key in seen:
            continue
        seen.add(key)
        latest.append(payload)

    for payload in reversed(latest):
        await websocket.send_text(payload)

    return entries[0][0]


async def _pump(websocket: WebSocket) -> None:
    """Redis -> browser. Telemetry, decisions and control frames interleave on
    one socket, which is what the client's event_type switch already expects.

    Order matters. SYSTEM_READY goes first so the client flushes before it
    repopulates, and the control snapshot goes LAST so action verdicts retire
    conflicts that the decision snapshot has just replayed.
    """
    floor_id, ready_payload = await _boot_floor()

    if floor_id is None:
        cursors = {
            TELEMETRY_STREAM: "$",
            DECISION_STREAM: "$",
            CONTROL_STREAM: "0-0",
        }
    else:
        if ready_payload:
            await websocket.send_text(ready_payload)
        cursors = {
            TELEMETRY_STREAM: await _snapshot_since(websocket, TELEMETRY_STREAM, floor_id),
            DECISION_STREAM: await _snapshot_since(websocket, DECISION_STREAM, floor_id),
            CONTROL_STREAM: await _snapshot_since(websocket, CONTROL_STREAM, floor_id),
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
            f"for {action.get('conflict_id')} @ {action.get('epoch') or '<unset>'}"
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