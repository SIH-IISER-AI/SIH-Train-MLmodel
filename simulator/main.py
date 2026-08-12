"""Simulator process: source of truth for topology, fleet, and telemetry.

Three concerns run here:

    boot           parse data files, validate, commit static state to Redis,
                   signal SYSTEM_READY
    main thread    tick -> publish telemetry + clock, sleep
    consumer       block on XREAD(action_stream), submit directives, publish the
                   verdict so the dashboard is not guessing
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import redis

from railsim.contracts import (
    ControllerActionResult,
    SimulationTick,
    SystemReady,
    TrainTelemetry,
)
from railsim.state import CONTROL_STREAM, commit_static_state, compute_epoch
from injector import LiveTelemetryInjector

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

TELEMETRY_STREAM = "telemetry_stream"
ACTION_STREAM = "action_stream"
DECISION_STREAM = "decision_stream"

NETWORK_PATH = os.getenv("NETWORK_PATH", "/app/data/network.json")
SCENARIO_PATH = os.getenv("SCENARIO_PATH", "/app/data/scenario.json")
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.0"))
TIME_MULTIPLIER = int(os.getenv("TIME_MULTIPLIER", "5"))

REDIS_BOOT_TIMEOUT_S = float(os.getenv("REDIS_BOOT_TIMEOUT_S", "60"))

#: control_stream now carries SYSTEM_READY and every action verdict. It must be
#: deep enough that a run's SYSTEM_READY is never trimmed away by later results,
#: because ws-server uses it as the epoch floor for backfill.
CONTROL_STREAM_MAXLEN = 500

_stop = threading.Event()


class ActionConsumer(threading.Thread):
    """Blocks on the action stream and turns CONTROLLER_ACTION into directives.

    The payload carries conflict_id + scenario_id, NOT a train_id -- so this
    thread also caches DISPATCH_RECOMMENDATION messages off the decision stream
    and looks up the directives the chosen scenario resolves to.

    Every path into the injector is fenced by epoch. Directives are positions
    frozen at solve time; applying one computed against a previous run's
    positions is worse than applying nothing.

    Every terminal path publishes a CONTROLLER_ACTION_RESULT. The controller
    pressed a button; they are owed an answer whichever way it went.
    """

    daemon = True

    def __init__(
        self,
        client: redis.Redis,
        injector: LiveTelemetryInjector,
        epoch: str,
    ) -> None:
        super().__init__(name="action-consumer")
        self.client = client
        self.injector = injector
        self.epoch = epoch
        self.scenario_directives: Dict[str, list] = {}
        self._warned_epochs: set = set()

    def run(self) -> None:
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

    def _publish_result(
        self,
        conflict_id: str,
        scenario_id: str,
        outcome: str,
        reason: str = "",
        directives_applied: int = 0,
    ) -> None:
        result = ControllerActionResult(
            conflict_id=conflict_id,
            scenario_id=scenario_id,
            epoch=self.epoch,
            outcome=outcome,
            reason=reason,
            directives_applied=directives_applied,
            timestamp=int(time.time() * 1000),
        )
        try:
            self.client.xadd(
                CONTROL_STREAM,
                {"payload": result.model_dump_json()},
                maxlen=CONTROL_STREAM_MAXLEN,
                approximate=True,
            )
        except redis.RedisError as exc:
            print(f"[actions] could not publish result: {exc}")

    def _warn_foreign_epoch(self, epoch: str, context: str) -> None:
        marker = f"{context}:{epoch}"
        if marker in self._warned_epochs:
            return
        self._warned_epochs.add(marker)
        print(
            f"[actions] discarding {context} from epoch={epoch or '<unset>'}; "
            f"current epoch={self.epoch}"
        )

    def _cache_recommendation(self, event: Dict[str, Any]) -> None:
        if event.get("event_type") != "DISPATCH_RECOMMENDATION":
            return
        event_epoch = event.get("epoch", "")
        if event_epoch != self.epoch:
            self._warn_foreign_epoch(event_epoch, "recommendation")
            return
        conflict_id = event.get("conflict_id")
        for scenario in event.get("scenarios", []):
            key = f"{conflict_id}:{scenario['scenario_id']}"
            self.scenario_directives[key] = scenario.get("directives", [])

    def _lookup_from_stream(self, conflict_id: str, scenario_id: str) -> Optional[list]:
        """Recover a scenario's directives from the durable decision stream.

        The in-memory cache is empty after a simulator restart and is not shared
        between replicas, so a miss re-reads rather than guessing. Entries from
        any other epoch are invisible to this lookup.
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
            if event.get("epoch", "") != self.epoch:
                continue
            if event.get("conflict_id") != conflict_id:
                continue
            for scenario in event.get("scenarios", []):
                if scenario.get("scenario_id") == scenario_id:
                    return scenario.get("directives", [])
        return None

    def _already_in_force(self, directives: list) -> bool:
        """Is this exact plan already executing on the fleet right now?

        Re-applying a HOLD_AT_LOOP is not a harmless duplicate. `_drain_directives`
        recomputes `hold_expires_sim_s` from the CURRENT sim clock, so a second
        press silently extends the hold by another full `max_hold_seconds` and
        wipes any regulation. Exactly-once has to be enforced here, at the sink,
        because it is the only place that cannot be defeated by a timing race
        upstream.
        """
        if not directives:
            return False
        for directive in directives:
            train = self.injector.trains.get(str(directive.get("train_id", "")))
            if train is None:
                return False
            kind = str(directive.get("kind", "HOLD_AT_LOOP")).upper()
            if kind == "HOLD_AT_LOOP":
                station = directive.get("station_id")
                loop = directive.get("loop_id")
                if station is None and loop is None:
                    return False
                running_to_hold = station is not None and train.hold_station_id == station
                standing_in_loop = loop is not None and train.in_loop == loop
                if not (running_to_hold or standing_in_loop):
                    return False
            elif kind == "STAND_ON_MAIN":
                if not train.standing_on_main:
                    return False
                if train.hold_station_id != directive.get("station_id"):
                    return False
            elif kind == "REGULATE":
                target = directive.get("target_speed_kmh")
                if target is None or train.regulated_to_kmh != float(target):
                    return False
            else:
                return False
        return True

    def _apply_action(self, event: Dict[str, Any]) -> None:
        if event.get("event_type") != "CONTROLLER_ACTION":
            return
        conflict_id = event.get("conflict_id")
        scenario_id = event.get("scenario_id")
        key = f"{conflict_id}:{scenario_id}"

        action_epoch = event.get("epoch", "")
        if action_epoch and action_epoch != self.epoch:
            reason = (
                f"raised against epoch {action_epoch}, simulator is on {self.epoch}"
            )
            print(f"[actions] REJECTED {key}: {reason}. Nothing applied.")
            self._publish_result(conflict_id, scenario_id, "rejected", reason)
            return

        if key in self.scenario_directives:
            directives = self.scenario_directives[key]
        else:
            directives = self._lookup_from_stream(conflict_id, scenario_id)
            if directives is not None:
                self.scenario_directives[key] = directives

        # A scenario that resolves to no directives is a real plan -- "the
        # natural order already works". Not finding the scenario at all is a
        # different outcome and must not be silently treated as that plan.
        if directives is None:
            reason = f"no scenario found for epoch {self.epoch}"
            print(f"[actions] REJECTED {key}: {reason}. Nothing applied.")
            self._publish_result(conflict_id, scenario_id, "rejected", reason)
            return

        if not directives:
            print(f"[actions] {key} -> no-op scenario, nothing to apply")
            self._publish_result(
                conflict_id, scenario_id, "no_op",
                "this scenario contains no directives; nothing was applied",
            )
            return

        if self._already_in_force(directives):
            print(f"[actions] {key} -> already in force, nothing re-applied")
            self._publish_result(
                conflict_id, scenario_id, "no_op",
                "this plan is already in force",
                directives_applied=0,
            )
            return

        accepted = [self.injector.submit_directive(d) for d in directives]
        if not all(accepted):
            for directive in directives:
                self.injector.submit_directive(
                    {"kind": "RELEASE", "train_id": directive.get("train_id")}
                )
            reason = f"{accepted.count(False)} directive(s) unrecognised, rolled back"
            print(f"[actions] REJECTED {key}: {reason}.")
            self._publish_result(conflict_id, scenario_id, "rejected", reason)
            return

        for directive in directives:
            print(f"[actions] {key} -> {directive.get('kind')} "
                  f"{directive.get('train_id')} applied")
        self._publish_result(
            conflict_id, scenario_id, "applied",
            directives_applied=len(directives),
        )


def publish(client: redis.Redis, stream: str, model) -> None:
    client.xadd(stream, {"payload": model.model_dump_json()}, maxlen=5000, approximate=True)


def wait_for_redis(client: redis.Redis, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            client.ping()
            return
        except redis.RedisError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Redis unreachable after {timeout_s}s: {exc}") from exc
            print("[sim] waiting for redis...")
            time.sleep(1.0)


def main() -> None:
    network = json.loads(Path(NETWORK_PATH).read_text(encoding="utf-8"))
    scenario = json.loads(Path(SCENARIO_PATH).read_text(encoding="utf-8"))
    epoch = compute_epoch(network, scenario)

    injector = LiveTelemetryInjector(
        network=network,
        scenario=scenario,
        tick_seconds=TICK_SECONDS,
        time_multiplier=TIME_MULTIPLIER,
    )

    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    wait_for_redis(client, REDIS_BOOT_TIMEOUT_S)

    commit_static_state(client, network, scenario, epoch)

    ready = SystemReady(
        epoch=epoch,
        timestamp=int(time.time() * 1000),
        section_id=injector.topology.section_id,
        train_ids=sorted(injector.trains.keys()),
        tick_seconds=TICK_SECONDS,
        time_multiplier=TIME_MULTIPLIER,
    )
    client.xadd(
        CONTROL_STREAM,
        {"payload": ready.model_dump_json()},
        maxlen=CONTROL_STREAM_MAXLEN, approximate=True,
    )
    print(f"[sim] static state committed, epoch={epoch}, "
          f"{len(injector.trains)} trains on {injector.topology.section_id}")

    ActionConsumer(client, injector, epoch).start()
    print(f"[sim] running at {TICK_SECONDS}s/tick x{TIME_MULTIPLIER}, redis {REDIS_HOST}")

    def shutdown(*_):
        _stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while not _stop.is_set():
        started = time.monotonic()
        try:
            events = injector.tick()
            publish(client, TELEMETRY_STREAM, SimulationTick(**injector.tick_event(), epoch=epoch))
            for event in events:
                publish(client, TELEMETRY_STREAM, TrainTelemetry(**event))
        except redis.RedisError as exc:
            print(f"[sim] redis error, continuing: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[sim] tick failed: {exc!r}")

        _stop.wait(max(0.0, TICK_SECONDS - (time.monotonic() - started)))

    print("[sim] stopped")


if __name__ == "__main__":
    main()