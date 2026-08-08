"""AI engine process: telemetry in, conflict alerts and dispatch advice out.

This is the loop that was missing. `optimize_precedence()` resolves a conflict
that has already been packaged; nothing was doing the packaging, so the solver
was an island. Here:

    telemetry_stream -> ConflictDetector.ingest()
                     -> detect_grouped()       spatial-temporal projection
                     -> optimiser_inputs()     package the worst contested resource
                     -> optimize_precedence()  CP-SAT
                     -> decision_stream        CONFLICT_PREDICTED + advice

Recommendations carry `directives`, the machine-executable form of each
scenario. The simulator caches them, so the controller's CONTROLLER_ACTION
(which only names a scenario_id) resolves back to real train commands.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from typing import Any, Dict

import redis

from railsim.contracts import ConflictAlert, DispatchRecommendation, Scenario
from detector import ConflictDetector
from optimizer import optimize_precedence

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
NETWORK_PATH = os.getenv("NETWORK_PATH", "/app/shared/network.json")
SCENARIO_PATH = os.getenv("SCENARIO_PATH", "/app/shared/scenario.json")

TELEMETRY_STREAM = "telemetry_stream"
DECISION_STREAM = "decision_stream"

#: Re-publishing the same conflict every 2 s would bury the controller. One
#: alert per resource per cooldown, refreshed only if severity changes.
ALERT_COOLDOWN_S = float(os.getenv("ALERT_COOLDOWN_S", "60"))

#: Only solve for conflicts inside this horizon. Anything further out will be
#: re-detected with better data before it matters.
SOLVE_WITHIN_S = int(os.getenv("SOLVE_WITHIN_S", "1500"))

_stop = threading.Event()


def conflict_id_for(conflict: Dict[str, Any]) -> str:
    """Stable id for the same trains-on-resource, so alerts dedupe across ticks."""
    key = f"{conflict['resource_id']}|{'|'.join(sorted(conflict['conflicting_train_ids']))}"
    return "CONF-" + hashlib.sha1(key.encode()).hexdigest()[:8].upper()


class Engine:
    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self.detector = ConflictDetector(NETWORK_PATH, SCENARIO_PATH)
        self.last_alert: Dict[str, float] = {}
        self.last_severity: Dict[str, str] = {}

    def publish(self, model) -> None:
        self.client.xadd(
            DECISION_STREAM,
            {"payload": model.model_dump_json()},
            maxlen=2000, approximate=True,
        )

    def evaluate(self) -> None:
        now = time.monotonic()
        for conflict in self.detector.detect_grouped():
            if conflict["predicted_time_to_conflict_seconds"] > SOLVE_WITHIN_S:
                continue
            # An unactionable conflict still warrants an alert, but there is no
            # point running the solver: the train that would give way is already
            # committed to the resource.
            if not conflict["actionable"]:
                continue

            conflict_id = conflict_id_for(conflict)
            severity = conflict["severity"]
            changed = self.last_severity.get(conflict_id) != severity
            cooled = now - self.last_alert.get(conflict_id, -1e9) >= ALERT_COOLDOWN_S
            if not (changed or cooled):
                continue

            self.last_alert[conflict_id] = now
            self.last_severity[conflict_id] = severity

            self.publish(
                ConflictAlert(
                    conflict_id=conflict_id,
                    severity=severity,
                    predicted_time_to_conflict_seconds=conflict[
                        "predicted_time_to_conflict_seconds"
                    ],
                    location={
                        "section_id": "NDLS-AGC-04",
                        "junction_id": conflict["entry_station_a"],
                        "track_id": conflict["resource_id"],
                    },
                    conflicting_train_ids=conflict["conflicting_train_ids"],
                    root_cause=conflict["root_cause"],
                    estimated_cascading_impact_minutes=conflict[
                        "estimated_cascading_impact_minutes"
                    ],
                )
            )

            trains_in_conflict, track_topology = self.detector.optimiser_inputs(conflict)
            try:
                scenarios = optimize_precedence(trains_in_conflict, track_topology)
            except Exception as exc:  # noqa: BLE001
                # A solver failure must not take the engine down. The alert has
                # already gone out, and a controller with a warning and no advice
                # is still better off than one with neither.
                print(f"[ai] solver failed for {conflict_id}: {exc!r}")
                continue

            if not scenarios:
                continue

            self.publish(
                DispatchRecommendation(
                    conflict_id=conflict_id,
                    scenarios=[Scenario(**scenario) for scenario in scenarios],
                )
            )
            print(
                f"[ai] {conflict_id} {severity} "
                f"T-{conflict['predicted_time_to_conflict_seconds']}s "
                f"-> {scenarios[0]['action']}"
            )


def main() -> None:
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    engine = Engine(client)

    def shutdown(*_):
        _stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    cursor = {TELEMETRY_STREAM: "$"}
    print(f"[ai] engine up, redis {REDIS_HOST}")

    while not _stop.is_set():
        try:
            messages = client.xread(cursor, count=200, block=1000)
        except redis.RedisError as exc:
            print(f"[ai] redis error, retrying: {exc}")
            time.sleep(1.0)
            continue

        saw_tick = False
        for _, entries in messages:
            for message_id, fields in entries:
                cursor[TELEMETRY_STREAM] = message_id
                payload = fields.get("payload")
                if not payload:
                    continue
                event = json.loads(payload)
                if event.get("event_type") == "TRAIN_TELEMETRY":
                    engine.detector.ingest(event)
                elif event.get("event_type") == "SIMULATION_TICK":
                    saw_tick = True

        # Evaluate once per simulation tick, after that tick's telemetry has been
        # absorbed -- not once per message, which would run the solver forty
        # times on partially-updated state.
        if saw_tick:
            engine.evaluate()

    print("[ai] stopped")


if __name__ == "__main__":
    main()