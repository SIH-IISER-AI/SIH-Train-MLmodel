"""Deterministic train motion with real movement authority.

What changed and why
--------------------
The previous version set a signal aspect from block occupancy and used it only
to cap speed. That has two fatal consequences:

  1. A train facing an occupied block SLOWED but never STOPPED, so it eventually
     entered the block anyway. Trains passed through each other.
  2. Occupancy was keyed on (direction, block). On a single line that means an
     UP train and a DOWN train on the same rail hold different keys and never
     see each other at all.

This version gives every train a MOVEMENT AUTHORITY: the route distance beyond
which it may not proceed, computed from the first resource ahead that another
train holds. Position is hard-clamped to that limit, so a train physically
cannot enter an occupied resource. Aspect is then derived from the distance to
the authority limit, which is the correct causal direction -- the signal
reflects the authority, it does not create it.

Trains are advanced in descending priority order and occupancy is updated as
each one moves, so two trains cannot both claim a free section in the same tick.
That ordering is a greedy first-come/priority rule -- deliberately naive. It is
the baseline the OR engine is supposed to beat.
"""

from __future__ import annotations

import json

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

import railsim.kinematics as kin
from railsim.topology import Leg, Topology

DEFAULT_SEED_PATH = Path(__file__).with_name("seed_data.json")

MINIMAL_KEYS = (
    "event_type", "train_id", "current_block_id",
    "speed_kmh", "delay_seconds", "priority_weight",
)

ASPECT_GREEN = "GREEN"
ASPECT_DOUBLE_YELLOW = "DOUBLE_YELLOW"
ASPECT_YELLOW = "YELLOW"
ASPECT_RED = "RED"

#: Distance from the authority limit at which the protecting signal reads RED.
SIGNAL_OVERLAP_KM = 0.15

#: Forward scan granularity when looking for the first occupied resource.
PROBE_STEP_KM = 0.25

#: Minimum scan distance, so a stationary train still sees the section ahead.
MIN_LOOKAHEAD_KM = 6.0


@dataclass
class TrainRuntime:
    train_id: str
    train_name: str
    train_type: str
    priority_weight: float
    max_speed_kmh: float
    scheduled_speed_kmh: float
    train_length_m: float
    decel_ms2: float
    accel_ms2: float
    route: List[str]
    legs: List[Leg]
    route_length_km: float
    station_km: Dict[str, float]
    distance_km: float
    scheduled_distance_km: float
    speed_kmh: float
    initial_delay_seconds: int
    signal_aspect: str = ASPECT_GREEN
    authority_km: float = 0.0

    hold_station_id: Optional[str] = None
    hold_loop_id: Optional[str] = None
    regulated_to_kmh: Optional[float] = None
    standing_since_tick: Optional[int] = None
    #: Loop the train is standing IN. While set, it holds the loop as its
    #: resource and holds NOTHING on the running line.
    in_loop: Optional[str] = None
    #: Hold persists until THIS train has passed the holding station. Set by the
    #: optimiser, which knows who is being given precedence.
    hold_until_train_id: Optional[str] = None
    hold_expires_sim_s: Optional[float] = None

    position: Optional[Position] = field(default=None, repr=False)

    def occupied_resources(self, topology: Topology) -> Set[str]:
        """Head resource plus the tail's resource while the rake is straddling.

        A train standing in a loop occupies the LOOP and frees the running line.
        That is the entire operational purpose of a loop, and modelling a held
        train as still sitting on the main turns every "hold at loop"
        recommendation into a self-defeating one.
        """
        if self.in_loop is not None:
            return {self.in_loop}
        head = topology.resolve(self.legs, self.distance_km)
        tail_km = max(0.0, self.distance_km - self.train_length_m / 1000.0)
        tail = topology.resolve(self.legs, tail_km)
        return {head.resource_id, tail.resource_id}


class LiveTelemetryInjector:
    """Advances a fleet and produces contract-shaped events.

    Thread safety: tick() and submit_directive() are called from different
    threads (the main loop and the Redis action consumer), so both take the
    same lock. Directives are queued and applied at the top of the next tick
    rather than mid-advance, which keeps every tick a consistent snapshot.
    """

    def __init__(
        self,
        network_path: str | Path,
        scenario_path: str | Path,
        tick_seconds: float = 2.0,
        time_multiplier: int = 5,
        start_epoch_ms: Optional[int] = None,
        recycle_at_terminus: bool = True,
    ) -> None:
        self.topology = Topology.from_file(network_path)
        scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
        
        self.tick_seconds = float(tick_seconds)
        self.time_multiplier = int(time_multiplier)
        self.recycle_at_terminus = bool(recycle_at_terminus)

        self.tick_id = 0
        self.sim_epoch_ms = start_epoch_ms if start_epoch_ms is not None else self._seed_epoch(scenario)
        self.elapsed_sim_seconds = 0.0

        self._lock = threading.RLock()
        self._pending: List[Dict[str, Any]] = []
        self.applied_directives: List[Dict[str, Any]] = []

        self.trains: Dict[str, TrainRuntime] = {}
        for raw in scenario["trains"]:
            legs = self.topology.build_legs(raw["route"])
            profile = kin.profile_for(raw["train_type"])
            station_km = {
                (leg.link.from_id if leg.direction == "DOWN" else leg.link.to_id):
                leg.route_start_km for leg in legs
            }
            station_km[raw["route"][-1]] = self.topology.route_length_km(legs)

            runtime = TrainRuntime(
                train_id=str(raw["train_id"]),
                train_name=raw["train_name"],
                train_type=raw["train_type"],
                priority_weight=float(raw["priority_weight"]),
                max_speed_kmh=float(raw["max_speed_kmh"]),
                scheduled_speed_kmh=float(raw["scheduled_speed_kmh"]),
                train_length_m=float(raw.get("train_length_m", profile.train_length_m)),
                decel_ms2=profile.service_decel_ms2,
                accel_ms2=profile.accel_ms2,
                route=list(raw["route"]),
                legs=legs,
                route_length_km=self.topology.route_length_km(legs),
                station_km=station_km,
                distance_km=float(raw.get("start_offset_km", 0.0)),
                scheduled_distance_km=float(raw.get("start_offset_km", 0.0)),
                speed_kmh=float(raw["scheduled_speed_kmh"]),
                initial_delay_seconds=int(raw.get("initial_delay_seconds", 0)),
            )
            runtime.position = self.topology.resolve(legs, runtime.distance_km)
            runtime.authority_km = runtime.route_length_km
            self.trains[runtime.train_id] = runtime

        self._assert_legal_start()

    def _assert_legal_start(self) -> None:
        """Two trains may not begin inside the same interlocking resource.

        A seed that violates this is not a simulation bug, it is bad data, and
        no amount of movement-authority logic can repair it after the fact.
        """
        claimed: Dict[str, str] = {}
        for train in self.trains.values():
            for resource in train.occupied_resources(self.topology):
                other = claimed.get(resource)
                if other is not None and other != train.train_id:
                    raise ValueError(
                        f"Illegal seed: {train.train_id} and {other} both start "
                        f"inside {resource}."
                    )
                claimed[resource] = train.train_id

    @staticmethod
    def _seed_epoch(seed: dict) -> int:
        raw = seed.get("meta", {}).get("sim_start_iso")
        if not raw:
            return int(datetime.now().timestamp() * 1000)
        return int(datetime.fromisoformat(raw).timestamp() * 1000)

    @property
    def sim_seconds_per_tick(self) -> float:
        return self.tick_seconds * self.time_multiplier

    @property
    def active_train_count(self) -> int:
        return sum(1 for t in self.trains.values() if not t.position.at_terminus)

    # -- controller interface ---------------------------------------------

    def submit_directive(self, directive: Dict[str, Any]) -> bool:
        """Queue a CONTROLLER_ACTION directive. Safe to call from any thread.

        Accepted shapes:
            {"kind": "HOLD_AT_LOOP", "train_id": "40201",
             "station_id": "PWL", "loop_id": "LOOP-PWL-01",
             "until_train_id": "12626", "max_hold_seconds": 2400}
            {"kind": "REGULATE", "train_id": "12626", "target_speed_kmh": 72}
            {"kind": "RELEASE",  "train_id": "40201"}
        """
        train_id = str(directive.get("train_id", ""))
        if train_id not in self.trains:
            return False
        with self._lock:
            self._pending.append(dict(directive))
        return True

    def hold(self, train_id: str, station_id: Optional[str] = None) -> bool:
        """Convenience wrapper. Holds at the next station with a fitting loop."""
        return self.submit_directive(
            {"kind": "HOLD_AT_LOOP", "train_id": train_id, "station_id": station_id}
        )

    def _drain_directives(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []

        for directive in pending:
            train = self.trains.get(str(directive.get("train_id", "")))
            if train is None:
                continue
            kind = str(directive.get("kind", "HOLD_AT_LOOP")).upper()

            if kind == "RELEASE":
                train.hold_station_id = None
                train.hold_loop_id = None
                train.hold_until_train_id = None
                train.hold_expires_sim_s = None
                train.in_loop = None
                train.regulated_to_kmh = None
                train.standing_since_tick = None
            elif kind == "REGULATE":
                train.regulated_to_kmh = float(directive["target_speed_kmh"])
                train.hold_station_id = None
            else:
                station_id = directive.get("station_id") or self._next_loop_station(train)
                if station_id is None:
                    continue
                loop = self.topology.loop_at(station_id, train.train_length_m)
                train.hold_station_id = station_id
                train.hold_loop_id = directive.get("loop_id") or (loop.id if loop else None)
                train.hold_until_train_id = directive.get("until_train_id")
                train.hold_expires_sim_s = self.elapsed_sim_seconds + float(
                    directive.get("max_hold_seconds", 1800)
                )
                train.regulated_to_kmh = None

            self.applied_directives.append({**directive, "tick_id": self.tick_id})

    def _next_loop_station(self, train: TrainRuntime) -> Optional[str]:
        """First station ahead of the train that has a loop the rake fits in."""
        candidates = sorted(
            (km, station) for station, km in train.station_km.items()
            if km > train.distance_km + 0.5
        )
        for _, station in candidates:
            if self.topology.loop_at(station, train.train_length_m) is not None:
                return station
        return None

    # -- simulation --------------------------------------------------------

    def tick(self) -> List[dict]:
        with self._lock:
            self._drain_directives()

            step = self.sim_seconds_per_tick
            self.tick_id += 1
            self.elapsed_sim_seconds += step
            self.sim_epoch_ms += int(step * 1000)

            # resource -> holder. Rebuilt each tick, then mutated as trains move,
            # so a resource freed earlier in this tick becomes available to a
            # later train and a resource just taken is not double-claimed.
            occupancy: Dict[str, str] = {}
            for train in self.trains.values():
                for resource in train.occupied_resources(self.topology):
                    occupancy[resource] = train.train_id

            # Highest priority moves first. This is the naive greedy rule the
            # optimiser exists to improve on -- locally sensible, globally
            # suboptimal, which is exactly the demo's premise.
            for train in sorted(self.trains.values(), key=lambda t: -t.priority_weight):
                for resource in train.occupied_resources(self.topology):
                    occupancy.pop(resource, None)

                train.authority_km = self._movement_authority(train, occupancy)
                self._advance(train, step, occupancy)
                self._set_aspect(train)

                for resource in train.occupied_resources(self.topology):
                    occupancy[resource] = train.train_id

            return [self._to_event(train) for train in self.trains.values()]

    def stream(self) -> Iterator[List[dict]]:
        while True:
            yield self.tick()

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [self._to_event(train) for train in self.trains.values()]

    def _movement_authority(self, train: TrainRuntime, occupancy: Dict[str, str]) -> float:
        """Route distance beyond which this train may not proceed.

        Scans forward for the first resource held by another train and returns
        the distance at which that resource BEGINS -- the protecting signal.
        On a single line that resource is the whole station-to-station section,
        so an opposing train anywhere in it stops this train at the section
        entrance, which is where a crossing is actually made.
        """
        speed_ms = kin.kmh_to_ms(max(train.speed_kmh, 30.0))
        lookahead = max(
            MIN_LOOKAHEAD_KM,
            kin.braking_distance_m(speed_ms, train.decel_ms2) / 1000.0 * 2.0,
        )

        limit = train.route_length_km

        # An accepted hold is itself an authority limit: the train runs to the
        # holding station and stands there. It does NOT stop where it is.
        if train.hold_station_id is not None and train.in_loop is None:
            hold_km = train.station_km.get(train.hold_station_id)
            if hold_km is not None and hold_km >= train.distance_km - 0.05:
                limit = min(limit, hold_km)

        probe = PROBE_STEP_KM
        while probe <= lookahead:
            target = train.distance_km + probe
            if target >= train.route_length_km:
                break
            ahead = self.topology.resolve(train.legs, target)
            holder = occupancy.get(ahead.resource_id)
            if holder is not None and holder != train.train_id:
                # Stop SHORT of the resource boundary, not on it. A train
                # standing exactly at resource_start_km resolves as already
                # inside that resource -- which is how the collision invariant
                # was violated by a train that had correctly braked. The overlap
                # is also where the protecting signal really sits.
                return min(
                    limit,
                    max(train.distance_km, ahead.resource_start_km - SIGNAL_OVERLAP_KM),
                )
            probe += PROBE_STEP_KM

        return limit

    def _advance(self, train: TrainRuntime, step_seconds: float, occupancy=None) -> None:
        # Recycling is a teleport to km 0. Doing it blindly drops a train on top
        # of whoever is standing at the origin, which is how the invariant used
        # to break after the first train completed its route.
        if self.recycle_at_terminus and train.position.at_terminus:
            origin = self.topology.resolve(train.legs, 0.0)
            holder = (occupancy or {}).get(origin.resource_id)
            if holder is None or holder == train.train_id:
                train.distance_km = 0.0
                train.scheduled_distance_km = 0.0
                train.speed_kmh = 0.0
                train.hold_station_id = None
                train.position = origin
            else:
                train.speed_kmh = 0.0
                return

        gap_km = max(0.0, train.authority_km - train.distance_km)

        # v_max = sqrt(2*a*d): the fastest a train may run and still stop at the
        # authority limit under service braking. This single expression replaces
        # the old "slow down a bit near a red" hand-wave.
        stopping_limit_kmh = kin.ms_to_kmh(
            math.sqrt(2.0 * train.decel_ms2 * max(0.0, gap_km * 1000.0))
        )

        line_limit = min(train.max_speed_kmh, self._permitted_speed(train))
        if train.regulated_to_kmh is not None:
            line_limit = min(line_limit, train.regulated_to_kmh)

        target = min(line_limit, stopping_limit_kmh)

        accel = kin.ms_to_kmh(train.accel_ms2) * step_seconds
        brake = kin.ms_to_kmh(train.decel_ms2) * step_seconds
        if target > train.speed_kmh:
            train.speed_kmh = min(target, train.speed_kmh + accel)
        else:
            train.speed_kmh = max(target, train.speed_kmh - brake)

        hours = step_seconds / 3600.0
        proposed = train.distance_km + train.speed_kmh * hours

        # Hard clamp. Even if the braking curve were imperfect, the train is
        # physically prevented from entering an occupied resource.
        train.distance_km = min(proposed, train.authority_km, train.route_length_km)
        if train.distance_km >= train.authority_km - 1e-9 and proposed > train.authority_km:
            train.speed_kmh = 0.0

        train.scheduled_distance_km = min(
            train.route_length_km,
            train.scheduled_distance_km + train.scheduled_speed_kmh * hours,
        )
        train.position = self.topology.resolve(train.legs, train.distance_km)
        train.speed_kmh = min(train.speed_kmh, train.position.link_max_speed_kmh)

        # Arrived at the holding station: divert into the loop.
        if train.hold_station_id is not None and train.hold_loop_id is not None:
            hold_km = train.station_km.get(train.hold_station_id)
            if hold_km is not None and train.distance_km >= hold_km - 0.35:
                train.distance_km = max(train.distance_km, hold_km - 0.05)
                train.in_loop = train.hold_loop_id
                train.speed_kmh = 0.0
                train.position = self.topology.resolve(train.legs, train.distance_km)

        if train.speed_kmh < 1.0:
            if train.standing_since_tick is None:
                train.standing_since_tick = self.tick_id
        else:
            train.standing_since_tick = None

        # A hold is a LATCH, not a momentary condition. Releasing it the moment
        # the road ahead looks clear defeats the purpose: the road looks clear
        # precisely because the train being given precedence has not arrived
        # yet, and the held train would pull out in front of it again.
        if train.in_loop is not None and self._hold_discharged(train):
            # Pulling out of a loop means re-occupying the running line AT the
            # station, not just the road beyond it. Checking only the road ahead
            # let a released train materialise on top of a train standing at the
            # same signal.
            head = self.topology.resolve(train.legs, train.distance_km)
            tail = self.topology.resolve(
                train.legs, max(0.0, train.distance_km - train.train_length_m / 1000.0)
            )
            occ = occupancy or {}
            main_clear = all(
                occ.get(resource) in (None, train.train_id)
                for resource in (head.resource_id, tail.resource_id)
            )
            if main_clear and train.authority_km > train.distance_km + 0.5:
                train.in_loop = None
                train.hold_station_id = None
                train.hold_loop_id = None
                train.hold_until_train_id = None
                train.hold_expires_sim_s = None

    def _hold_discharged(self, train: TrainRuntime) -> bool:
        """Has the reason for this hold gone away?"""
        if train.hold_expires_sim_s is not None and (
            self.elapsed_sim_seconds >= train.hold_expires_sim_s
        ):
            return True
        if train.hold_until_train_id is None:
            return False
        other = self.trains.get(train.hold_until_train_id)
        if other is None or train.hold_station_id is None:
            return True
        # Each train measures the holding station on its OWN route, so this
        # comparison is direction-agnostic and works for a crossing as well as
        # an overtake.
        marker = other.station_km.get(train.hold_station_id)
        if marker is None:
            return True
        return other.distance_km > marker + 1.0

    def _permitted_speed(self, train: TrainRuntime) -> float:
        speed_ms = kin.kmh_to_ms(max(train.speed_kmh, 30.0))
        braking_km = kin.braking_distance_m(speed_ms, train.decel_ms2) / 1000.0
        limit = train.position.link_max_speed_kmh
        probe = 0.5
        while probe <= braking_km:
            ahead = self.topology.resolve(train.legs, train.distance_km + probe)
            limit = min(limit, ahead.link_max_speed_kmh)
            if ahead.at_terminus:
                break
            probe += 0.5
        return limit

    def _set_aspect(self, train: TrainRuntime) -> None:
        """Aspect is DERIVED from movement authority, never the other way round."""
        gap_km = max(0.0, train.authority_km - train.distance_km)
        if gap_km <= SIGNAL_OVERLAP_KM:
            train.signal_aspect = ASPECT_RED
            return
        speed_ms = kin.kmh_to_ms(max(train.speed_kmh, 30.0))
        braking_km = kin.braking_distance_m(speed_ms, train.decel_ms2) / 1000.0
        if gap_km <= braking_km:
            train.signal_aspect = ASPECT_YELLOW
        elif gap_km <= braking_km * 2.5:
            train.signal_aspect = ASPECT_DOUBLE_YELLOW
        else:
            train.signal_aspect = ASPECT_GREEN

    # -- event construction ------------------------------------------------

    def _delay_seconds(self, train: TrainRuntime) -> int:
        shortfall_km = train.scheduled_distance_km - train.distance_km
        accrued = shortfall_km / train.scheduled_speed_kmh * 3600.0
        return max(0, int(round(train.initial_delay_seconds + accrued)))

    def _schedule_status(self, train: TrainRuntime, delay_seconds: int) -> str:
        if train.in_loop is not None or train.hold_station_id is not None or (
            train.speed_kmh < 1.0 and train.signal_aspect == ASPECT_RED
        ):
            return "HELD"
        if delay_seconds > 120:
            return "DELAYED"
        return "ON_TIME"

    def _eta_next_station_ms(self, train: TrainRuntime) -> int:
        if train.speed_kmh < 1.0:
            return self.sim_epoch_ms
        hours = train.position.km_to_next_station / train.speed_kmh
        return self.sim_epoch_ms + int(hours * 3600 * 1000)

    def _to_event(self, train: TrainRuntime) -> dict:
        position = train.position
        delay_seconds = self._delay_seconds(train)
        return {
            "event_type": "TRAIN_TELEMETRY",
            "train_id": train.train_id,
            "train_name": train.train_name,
            "train_type": train.train_type,
            "priority_weight": round(train.priority_weight, 2),
            "current_section_id": position.section_id,
            "current_block_id": position.block_id,
            "coordinates": {"lat": round(position.lat, 6), "lng": round(position.lng, 6)},
            "speed_kmh": round(train.speed_kmh, 1),
            "max_allowed_speed_kmh": round(
                min(train.max_speed_kmh, position.link_max_speed_kmh), 1
            ),
            "schedule_status": self._schedule_status(train, delay_seconds),
            "delay_seconds": delay_seconds,
            "next_station_id": position.next_station_id,
            "eta_next_station": self._eta_next_station_ms(train),
            "signal_aspect": train.signal_aspect,
            # ---- contract extension -----------------------------------------
            # The AI engine cannot project occupancy from lat/lng without
            # re-deriving the entire topology mapping. The simulator already
            # knows the answer; sending it removes a whole class of
            # reconstruction bugs.
            "route_progress_km": round(train.distance_km, 3),
            "resource_id": position.resource_id,
            "track_id": position.track_id,
            "direction": position.direction,
        }

    def tick_event(self) -> dict:
        healthy = sum(1 for t in self.trains.values() if self._delay_seconds(t) <= 300)
        return {
            "event_type": "SIMULATION_TICK",
            "timestamp": self.sim_epoch_ms,
            "tick_id": self.tick_id,
            "time_multiplier": self.time_multiplier,
            "active_train_count": self.active_train_count,
            "network_health_score": round(healthy / max(1, len(self.trains)) * 100, 1),
        }


def project_minimal(event: dict) -> dict:
    return {key: event[key] for key in MINIMAL_KEYS}