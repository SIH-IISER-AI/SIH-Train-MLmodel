"""AI engine process: telemetry in, conflict alerts and dispatch advice out.

    hydrate          block on rail:network_topology until the simulator primes it
    telemetry_stream -> ConflictDetector.ingest()
                     -> detect_grouped()       spatial-temporal projection
                     -> optimiser_inputs()     package the worst contested resource
                     -> optimize_precedence()  CP-SAT
                     -> decision_stream        CONFLICT_PREDICTED + advice

The engine owns no static data. Network and fleet come from Redis, tagged with
an epoch; if the epoch on an incoming tick stops matching the one hydrated from,
the engine rebuilds rather than projecting new trains onto an old map.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from typing import Any, Dict, Optional, Tuple

import redis

from railsim.contracts import ConflictAlert, DispatchRecommendation, Scenario
from railsim.state import (
    CONTROL_STREAM,
    HYDRATION_POLL_SECONDS,
    KEY_FLEET,
    read_static_state,
)
from detector import ConflictDetector, SEVERITY_BANDS
from optimizer import optimize_precedence

#: Day-5 reversibility switch. optimizer.py is never deleted; if the day-14
#: merge gate fails, ENGINE stays 'enumerate' and the global model becomes a
#: roadmap slide with measured numbers rather than a rollback.
#:
#: The switch is read here and applied ABOVE evaluate()'s conflict loop, not at
#: the optimize_precedence call site. The global model solves ONCE per evaluate
#: for every contested resource in the window and then decomposes back into
#: per-conflict cards; there is no per-conflict call to swap. See decision 3 in
#: docs/GLOBAL_MODEL_SPEC.md.
ENGINE = os.getenv("ENGINE", "enumerate").strip().lower()
if ENGINE not in ("enumerate", "global"):
    raise SystemExit(f"ENGINE={ENGINE!r}; expected 'enumerate' or 'global'")
optimize_global = None
if ENGINE == "global":
    from optimizer_global import optimize_global  # noqa: F811
    print("[ai] ENGINE=global selected: one CP-SAT model per evaluate, "
          "lexicographic descent by IR class, decomposed per decision 3")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

TELEMETRY_STREAM = "telemetry_stream"
DECISION_STREAM = "decision_stream"

#: Backstop expiry for a plan the engine can no longer observe on the fleet --
#: a pure REGULATE plan leaves no trace in telemetry. Without this, a plan would
#: be remembered forever and a genuine re-detection would read as IN_FORCE.
PLAN_TTL_S = float(os.getenv("PLAN_TTL_S", "1800"))

SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

#: De-escalation only. Escalation is never delayed by either of these.
DEESCALATION_MARGIN_S = 60.0
DEESCALATION_DWELL_S = 30.0

def _band_ceiling(severity: str) -> float:
    for threshold, name in SEVERITY_BANDS:
        if name == severity:
            return float(threshold)
    return float("inf")

#: Re-publishing the same conflict every 2 s would bury the controller. One
#: alert per resource per cooldown, refreshed only if severity changes.
ALERT_COOLDOWN_S = float(os.getenv("ALERT_COOLDOWN_S", "60"))

#: Only solve for conflicts inside this horizon. Anything further out will be
#: re-detected with better data before it matters.
SOLVE_WITHIN_S = int(os.getenv("SOLVE_WITHIN_S", "1500"))

_stop = threading.Event()


def conflict_id_for(conflict: Dict[str, Any]) -> str:
    """Stable id for a contention between the same trains.

    Keying on the resource as well was correct for a single line, where the
    resource is a 40 km section and does not change while the conflict lives.
    On a running line the resource is a block, so a following move mints a new
    id every time the pair advances -- one card, one Approve button, and one
    independently solved restriction per block. Twelve cards for one decision,
    each solved without knowledge of the other eleven.

    The trains in contention are the decision. The block they happen to meet in
    is where it surfaces.
    """
    key = "|".join(sorted(conflict["conflicting_train_ids"]))
    return "CONF-" + hashlib.sha1(key.encode()).hexdigest()[:8].upper()

def plan_fingerprint(directives: Any) -> Tuple:
    """Order-independent identity of a dispatch plan.

    Two solves that produce the same set of interventions on the same trains are
    the same plan even if the scenario is re-ranked or re-lettered between ticks.
    Comparing scenario_id instead would report DIVERGED every time OPT-1 and
    OPT-2 swapped places without any operational change.
    """
    items = []
    for directive in directives or []:
        kind = str(directive.get("kind", "")).upper()
        items.append(
            (
                kind,
                str(directive.get("train_id", "")),
                str(directive.get("station_id") or ""),
                str(directive.get("loop_id") or ""),
            )
        )
    return tuple(sorted(items))


def hold_train_ids(directives: Any) -> Tuple[str, ...]:
    return tuple(
        sorted(
            str(d.get("train_id", ""))
            for d in (directives or [])
            if str(d.get("kind", "")).upper() in ("HOLD_AT_LOOP", "STAND_ON_MAIN")
        )
    )


def solvable_conflicts(detector) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """The conflicts that reach the solver, plus the count at each filter stage.

    Extracted from evaluate() so measurement tooling cannot drift from
    production. Two day-2 defects came from a tool reimplementing this chain
    slightly differently and being masked by a tick where the rules agreed.
    """
    counts = {"raw": 0, "within_horizon": 0, "actionable": 0}
    candidates: Dict[str, Dict[str, Any]] = {}
    for conflict in detector.detect_grouped():
        counts["raw"] += 1
        if conflict["predicted_time_to_conflict_seconds"] > SOLVE_WITHIN_S:
            continue
        counts["within_horizon"] += 1
        if not conflict["actionable"]:
            continue
        counts["actionable"] += 1
        conflict_id = conflict_id_for(conflict)
        incumbent = candidates.get(conflict_id)
        if incumbent is None or (
            conflict["predicted_time_to_conflict_seconds"]
            < incumbent["predicted_time_to_conflict_seconds"]
        ):
            candidates[conflict_id] = conflict
    counts["distinct"] = len(candidates)
    return candidates, counts


class Engine:
    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self.epoch: str = ""
        self.detector: Optional[ConflictDetector] = None
        self.last_alert: Dict[str, float] = {}
        self.last_severity: Dict[str, Tuple[str, str]] = {}
        #: conflict_id -> scenario_id -> published directive list.
        self.published: Dict[str, Dict[str, Any]] = {}
        #: conflict_id -> the plan the controller accepted and that is running.
        self.plan_in_force: Dict[str, Dict[str, Any]] = {}
        self.severity_pending: Dict[str, Tuple[str, float]] = {}

    # -- hydration ---------------------------------------------------------

    def hydrate(self) -> bool:
        """Block until the simulator has committed a complete static state.

        Returns False only if shutdown was requested while waiting. Every other
        outcome retries: a missing key, a torn read, and a Redis outage are all
        the same situation from here -- the environment is not ready yet.
        """
        announced = False
        while not _stop.is_set():
            try:
                state = read_static_state(self.client)
            except redis.RedisError as exc:
                print(f"[ai] redis unavailable during hydration: {exc}")
                state = None

            if state is None:
                if not announced:
                    print("[ai] waiting for simulator to commit static state...")
                    announced = True
                _stop.wait(HYDRATION_POLL_SECONDS)
                continue

            epoch, network, fleet = state
            detector = ConflictDetector(network, fleet)

            self.epoch = epoch
            self.detector = detector
            self.last_alert.clear()
            self.last_severity.clear()
            self.published.clear()
            self.plan_in_force.clear()
            self.severity_pending.clear()

            print(
                f"[ai] hydrated epoch={epoch} section={detector.topology.section_id} "
                f"fleet={detector.fleet_size}"
            )
            return True
        return False

    def ensure_registered(self, train_id: str) -> bool:
        """Lazy fill for a train that appeared after hydration.

        The fleet hash is authoritative; a train absent from it is telemetry the
        engine has no route for and must ignore rather than guess at.
        """
        if self.detector.knows(train_id):
            return True
        try:
            blob = self.client.hget(KEY_FLEET, train_id)
        except redis.RedisError as exc:
            print(f"[ai] fleet lookup failed for {train_id}: {exc}")
            return False
        if not blob:
            return False
        try:
            entry = json.loads(blob)
        except json.JSONDecodeError:
            return False
        if self.detector.register_train(entry):
            print(f"[ai] late-registered {train_id} from fleet hash")
            return True
        return False

    # -- evaluation --------------------------------------------------------

    def publish(self, model) -> None:
        self.client.xadd(
            DECISION_STREAM,
            {"payload": model.model_dump_json()},
            maxlen=2000, approximate=True,
        )

    # -- plan tracking -----------------------------------------------------

    def note_action_result(self, event: Dict[str, Any]) -> None:
        """Record the plan the controller committed to.

        This is the channel that was missing. It does NOT gate detection or
        solving -- the engine keeps projecting and keeps enumerating precedence
        every tick. It only lets the engine distinguish "here is a decision you
        have not made" from "here is the decision you already made", which is
        the distinction the approve button was silently collapsing.
        """
        if event.get("event_type") != "CONTROLLER_ACTION_RESULT":
            return
        if event.get("epoch", "") != self.epoch:
            return
        conflict_id = str(event.get("conflict_id", ""))
        scenario_id = str(event.get("scenario_id", ""))
        outcome = event.get("outcome")

        if outcome == "rejected":
            return
        if outcome not in ("applied", "no_op"):
            return

        record = self.published.get(conflict_id, {})
        directives = record.get("scenarios", {}).get(scenario_id, [])

        self.plan_in_force[conflict_id] = {
            "scenario_id": scenario_id,
            "fingerprint": plan_fingerprint(directives),
            "hold_train_ids": hold_train_ids(directives),
            "train_ids": record.get("train_ids", ()),
            "at": time.monotonic(),
        }

    def _plan_still_live(self, conflict_id: str, plan: Dict[str, Any]) -> bool:
        """Is the accepted plan still executing on the fleet?

        A plan with holds is observable: the trains it names still report a hold
        station or a loop. A plan with only regulation leaves no trace in
        telemetry, so it falls back to a wall-clock backstop rather than being
        remembered indefinitely.
        """
        if time.monotonic() - float(plan.get("at", 0.0)) > PLAN_TTL_S:
            return False

        holds = plan.get("hold_train_ids") or ()
        if not holds:
            return True

        for train_id in holds:
            tracked = self.detector.trains.get(str(train_id))
            if tracked is None:
                continue
            if tracked.hold_station_id is not None or tracked.in_loop_id is not None:
                return True
        return False

    def _stable_severity(
        self, conflict_id: str, raw: str, contested_at: float, now: float
    ) -> str:
        """Escalate instantly; de-escalate only past a margin and a dwell.

        Flapping is by definition an oscillation across a band boundary, and an
        oscillation cannot complete without a de-escalation. Gating only that
        direction stops the flap while leaving the alarm path untouched -- a
        conflict that becomes CRITICAL is published on the tick it does.
        """
        previous = self.last_severity.get(conflict_id)
        previous_sev = previous[0] if previous else None

        if previous_sev is None or SEVERITY_RANK.get(raw, 0) >= SEVERITY_RANK.get(
            previous_sev, 0
        ):
            self.severity_pending.pop(conflict_id, None)
            return raw

        if contested_at < _band_ceiling(previous_sev) + DEESCALATION_MARGIN_S:
            self.severity_pending.pop(conflict_id, None)
            return previous_sev

        pending = self.severity_pending.get(conflict_id)
        if pending is None or pending[0] != raw:
            self.severity_pending[conflict_id] = (raw, now)
            return previous_sev
        if now - pending[1] < DEESCALATION_DWELL_S:
            return previous_sev

        self.severity_pending.pop(conflict_id, None)
        return raw
    
    

    def evaluate(self) -> None:
        now = time.monotonic()
        section_id = self.detector.topology.section_id

        # An unactionable conflict still warrants an alert, but there is no
        # point running the solver: the train that would give way is already
        # committed to the resource. See solvable_conflicts().
        candidates, _ = solvable_conflicts(self.detector)

        # ENGINE=global solves every raised conflict in ONE model, then hands
        # back the same conflict_id -> [scenario] mapping the per-conflict
        # engine produces. The card, the OPT-1/OPT-2 selector and the
        # controller's authority over which plan is executed are unchanged --
        # only where precedence is decided has moved.
        global_plans: Dict[str, Any] = {}
        if ENGINE == "global" and candidates:
            solve_started = time.monotonic()
            try:
                global_plans = optimize_global(self.detector, candidates)
            except Exception as exc:  # noqa: BLE001
                print(f"[ai] global solve failed: {exc!r}")
                global_plans = {}
            print(f"[ai] global solve {time.monotonic() - solve_started:.2f}s "
                  f"over {len(candidates)} conflict(s), "
                  f"{sum(len(v) for v in global_plans.values())} scenario(s)")

        for conflict_id, conflict in candidates.items():
            severity = self._stable_severity(
                conflict_id,
                conflict["severity"],
                float(conflict["predicted_time_to_conflict_seconds"]),
                now,
            )
            train_ids = tuple(sorted(conflict["conflicting_train_ids"]))

            # A plan is scoped to the train set it was solved for. A third train
            # joining the contention is a different decision, not the same one
            # re-offered, so the plan stops applying rather than masking it.
            plan = self.plan_in_force.get(conflict_id)
            if plan is not None:
                if plan.get("train_ids") and plan["train_ids"] != train_ids:
                    self.plan_in_force.pop(conflict_id, None)
                    plan = None
                elif not self._plan_still_live(conflict_id, plan):
                    self.plan_in_force.pop(conflict_id, None)
                    plan = None

            if ENGINE == "global":
                scenarios = global_plans.get(conflict_id, [])
            else:
                trains_in_conflict, track_topology = self.detector.optimiser_inputs(
                    conflict
                )
                try:
                    scenarios = optimize_precedence(
                        trains_in_conflict, track_topology
                    )
                except Exception as exc:  # noqa: BLE001
                    # A solver failure must not take the engine down. A
                    # controller with a warning and no advice is still better
                    # off than one with neither, so the alert is published
                    # either way.
                    print(f"[ai] solver failed for {conflict_id}: {exc!r}")
                    scenarios = []

            if plan is None:
                plan_state = "OPEN"
                plan_id = None
            else:
                plan_id = plan["scenario_id"]
                if scenarios:
                    best = plan_fingerprint(scenarios[0].get("directives"))
                    plan_state = (
                        "IN_FORCE" if best == plan["fingerprint"] else "DIVERGED"
                    )
                else:
                    plan_state = "IN_FORCE"

            # Plan state is part of the alert's identity for gating purposes.
            # A transition to DIVERGED is the most urgent thing this engine can
            # say and must not sit behind a cooldown started by a severity that
            # has not moved.
            state_key = (severity, plan_state)
            changed = self.last_severity.get(conflict_id) != state_key
            cooled = now - self.last_alert.get(conflict_id, -1e9) >= ALERT_COOLDOWN_S
            if not (changed or cooled):
                continue

            self.last_alert[conflict_id] = now
            self.last_severity[conflict_id] = state_key

            self.publish(
                ConflictAlert(
                    conflict_id=conflict_id,
                    epoch=self.epoch,
                    severity=severity,
                    predicted_time_to_conflict_seconds=conflict[
                        "predicted_time_to_conflict_seconds"
                    ],
                    location={
                        "section_id": section_id,
                        "junction_id": conflict["entry_station_a"],
                        "track_id": conflict["resource_id"],
                    },
                    conflicting_train_ids=conflict["conflicting_train_ids"],
                    root_cause=conflict["root_cause"],
                    estimated_cascading_impact_minutes=conflict[
                        "estimated_cascading_impact_minutes"
                    ],
                    plan_state=plan_state,
                    plan_in_force=plan_id,
                )
            )

            if not scenarios:
                continue

            

            self.published[conflict_id] = {
                "train_ids": train_ids,
                "scenarios": {
                    scenario["scenario_id"]: scenario.get("directives", [])
                    for scenario in scenarios
                },
            }

            self.publish(
                DispatchRecommendation(
                    conflict_id=conflict_id,
                    epoch=self.epoch,
                    scenarios=[Scenario(**scenario) for scenario in scenarios],
                )
            )
            print(
                f"[ai] {conflict_id} {severity} {plan_state} "
                f"T-{conflict['predicted_time_to_conflict_seconds']}s "
                f"-> {scenarios[0]['action']}"
            )


def main() -> None:
    def shutdown(*_):
        _stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    engine = Engine(client)

    if not engine.hydrate():
        print("[ai] stopped before hydration")
        return

    cursor = {TELEMETRY_STREAM: "$", CONTROL_STREAM: "$"}
    unknown_reported: set = set()
    print(f"[ai] engine up, redis {REDIS_HOST}")

    while not _stop.is_set():
        try:
            messages = client.xread(cursor, count=200, block=1000)
        except redis.RedisError as exc:
            print(f"[ai] redis error, retrying: {exc}")
            time.sleep(1.0)
            continue

        saw_tick = False
        stale = False

        for stream_name, entries in messages:
            for message_id, fields in entries:
                cursor[stream_name] = message_id
                payload = fields.get("payload")
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                kind = event.get("event_type")
                if stream_name == CONTROL_STREAM:
                    engine.note_action_result(event)
                    continue
                if kind == "SIMULATION_TICK":
                    tick_epoch = event.get("epoch", "")
                    if tick_epoch and tick_epoch != engine.epoch:
                        stale = True
                        break
                    saw_tick = True
                elif kind == "TRAIN_TELEMETRY":
                    train_id = str(event.get("train_id", ""))
                    try:
                        known = engine.detector.ingest(event)
                    except KeyError as exc:
                        if train_id not in unknown_reported:
                            print(f"[ai] malformed telemetry for {train_id}: {exc}")
                            unknown_reported.add(train_id)
                        continue
                    if not known:
                        if engine.ensure_registered(train_id):
                            try:
                                engine.detector.ingest(event)
                            except KeyError:
                                pass
                            unknown_reported.discard(train_id)
                        elif train_id not in unknown_reported:
                            print(f"[ai] telemetry for unregistered train {train_id}; ignoring")
                            unknown_reported.add(train_id)
            if stale:
                break

        if stale:
            # The simulator restarted or the data files changed. Every tracked
            # position, every leg, every dedupe entry belongs to the old run.
            # Rebuild rather than reconcile.
            print(f"[ai] epoch changed from {engine.epoch}; re-hydrating")
            if not engine.hydrate():
                break
            cursor[TELEMETRY_STREAM] = "$"
            cursor[CONTROL_STREAM] = "$"
            unknown_reported.clear()
            continue

        # Evaluate once per simulation tick, after that tick's telemetry has been
        # absorbed -- not once per message, which would run the solver forty
        # times on partially-updated state.
        if saw_tick:
            try:
                engine.evaluate()
            except Exception as exc:  # noqa: BLE001
                print(f"[ai] evaluation failed, continuing: {exc!r}")

    print("[ai] stopped")


if __name__ == "__main__":
    main()