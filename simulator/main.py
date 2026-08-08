"""Simulator process: publishes telemetry AND listens for controller actions.

The previous version only spoke. Everything after the controller's click --
the action_stream entry, the directive, the hold -- landed nowhere, so the
dashboard button was decorative.

Two loops now run concurrently:

    main thread    tick -> publish telemetry + clock, sleep
    consumer       block on XREAD(action_stream), submit directives

They share only `injector`, whose submit_directive() is thread-safe and queues
work applied at the top of the next tick. That keeps every published tick a
consistent snapshot instead of a half-applied one.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from typing import Any, Dict, Optional

import redis

from railsim.contracts import SimulationTick, TrainTelemetry
from injector import LiveTelemetryInjector
from injector import LiveTelemetryInjector

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))



TELEMETRY_STREAM = "telemetry_stream"
ACTION_STREAM = "action_stream"
DECISION_STREAM = "decision_stream"

NETWORK_PATH = os.getenv("NETWORK_PATH", "/app/shared/network.json")
SCENARIO_PATH = os.getenv("SCENARIO_PATH", "/app/shared/scenario.json")
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.0"))
TIME_MULTIPLIER = int(os.getenv("TIME_MULTIPLIER", "5"))

_stop = threading.Event()


class ActionConsumer(threading.Thread):
    """Blocks on the action stream and turns CONTROLLER_ACTION into directives.

    The payload carries conflict_id + scenario_id, NOT a train_id -- so this
    thread also caches DISPATCH_RECOMMENDATION messages off the decision stream
    and looks up the directives the chosen scenario resolves to. Without that
    cache the simulator receives an id it has no way to interpret.
    """

    daemon = True

    def __init__(self, client: redis.Redis, injector: LiveTelemetryInjector) -> None:
        super().__init__(name="action-consumer")
        self.client = client
        self.injector = injector
        self.scenario_directives: Dict[str, list] = {}

    def run(self) -> None:
        # "$" only ONCE, then track the last id. Re-reading with "$" every
        # iteration silently drops anything that arrived between reads.
        cursors = {ACTION_STREAM: "$", DECISION_STREAM: "$"}
        while not _stop.is_set():
            try:
                messages = self.client.xread(cursors, count=50, block=1000)
            except redis.RedisError as exc:
                print(f"[actions] redis error, retrying: {exc}")
                time.sleep(1.0)
                continue

            for stream_name, entries in messages:
                for message_id, fields in entries:
                    cursors[stream_name] = message_id
                    payload = fields.get("payload")
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if stream_name == DECISION_STREAM:
                        self._cache_recommendation(event)
                    else:
                        self._apply_action(event)

    def _cache_recommendation(self, event: Dict[str, Any]) -> None:
        if event.get("event_type") != "DISPATCH_RECOMMENDATION":
            return
        conflict_id = event.get("conflict_id")
        for scenario in event.get("scenarios", []):
            key = f"{conflict_id}:{scenario['scenario_id']}"
            self.scenario_directives[key] = scenario.get("directives", [])

    def _lookup_from_stream(self, conflict_id: str, scenario_id: str) -> Optional[list]:
        """Recover a scenario's directives from the decision stream.
        
        The in-memory cache is empty after a simulator restart and is not shared
        between replicas. Redis streams are durable and are the actual source of
        truth for what was recommended, so a miss re-reads rather than guessing.
        XREVRANGE walks newest-first; the first match wins.
        """
        try:
            entries = self.client.xrevrange(DECISION_STREAM, count=2000)
        except redis.RedisError as exc:
            print(f"[actions] stream lookup failed: {exc}")
            return None

        for _, fields in entries:
            payload = fields.get("payload")
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") != "DISPATCH_RECOMMENDATION":
                continue
            if event.get("conflict_id") != conflict_id:
                continue
            for scenario in event.get("scenarios", []):
                if scenario.get("scenario_id") == scenario_id:
                    return scenario.get("directives", [])
        return None

    def _apply_action(self, event: Dict[str, Any]) -> None:
        if event.get("event_type") != "CONTROLLER_ACTION":
            return
        conflict_id = event.get("conflict_id")
        scenario_id = event.get("scenario_id")
        key = f"{conflict_id}:{scenario_id}"

        directives = self.scenario_directives.get(key)
        if directives is None:
            directives = self._lookup_from_stream(conflict_id, scenario_id)
            if directives is not None:
                self.scenario_directives[key] = directives

        # NO synthesised fallback. A grouped scenario is a set of directives that
        # only makes sense together: holding one train of a four-train crossing
        # and not the other three produces a plan the controller never approved
        # and that measured WORSE than doing nothing. Rejecting is the safe
        # failure, and it is visible.
        if not directives:
            print(
                f"[actions] REJECTED {key}: no directives found in cache or "
                f"decision stream. Nothing applied."
            )
            return

        accepted = [self.injector.submit_directive(d) for d in directives]
        if not all(accepted):
            # Partial application is the same failure in a different costume.
            for directive in directives:
                self.injector.submit_directive(
                    {"kind": "RELEASE", "train_id": directive.get("train_id")}
                )
            print(f"[actions] REJECTED {key}: {accepted.count(False)} directive(s) "
                  f"unrecognised; rolled back the rest.")
            return

        for directive in directives:
            print(f"[actions] {key} -> {directive.get('kind')} "
                  f"{directive.get('train_id')} applied")


def publish(client: redis.Redis, stream: str, model) -> None:
    client.xadd(stream, {"payload": model.model_dump_json()}, maxlen=5000, approximate=True)


def main() -> None:
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    injector = LiveTelemetryInjector(
        network_path=NETWORK_PATH,
        scenario_path=SCENARIO_PATH,
        tick_seconds=TICK_SECONDS, 
        time_multiplier=TIME_MULTIPLIER
    )

    ActionConsumer(client, injector).start()
    print(f"[sim] running at {TICK_SECONDS}s/tick x{TIME_MULTIPLIER}, redis {REDIS_HOST}")

    def shutdown(*_):
        _stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while not _stop.is_set():
        started = time.monotonic()
        try:
            events = injector.tick()
            publish(client, TELEMETRY_STREAM, SimulationTick(**injector.tick_event()))
            for event in events:
                publish(client, TELEMETRY_STREAM, TrainTelemetry(**event))
        except redis.RedisError as exc:
            # A transient Redis hiccup must not kill the simulator. The old
            # `except Exception: break` turned one dropped packet into a dead
            # demo.
            print(f"[sim] redis error, continuing: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sim] tick failed: {exc!r}")

        _stop.wait(max(0.0, TICK_SECONDS - (time.monotonic() - started)))

    print("[sim] stopped")


if __name__ == "__main__":
    main()