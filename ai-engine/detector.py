"""Spatial-temporal conflict detection.

This is the piece that was missing: something that watches live telemetry,
projects where every train will be for the next N minutes, and works out which
pairs will demand the same interlocking resource at overlapping times.
`optimize_precedence()` only resolves a conflict; this finds one.

Method
------
For a train at route distance d running at v, the resources ahead lie in a
contiguous sequence along its route. Walking that sequence gives an exact
occupancy schedule at constant speed:

    t_in (r)  = (r.start - d) / v          head reaches the resource
    t_out(r)  = (r.end + L - d) / v        tail clears it

Two trains conflict when their [t_in, t_out] windows for the SAME resource_id
overlap, after allowing one signalling headway. On a double line resource_id is
(running line, block), so opposing trains never match. On a single line it is
the whole station-to-station section, so they do -- which is the head-on case
block-level detection cannot see.

Constant speed is a deliberate simplification. It is wrong in detail and right
in the way that matters: it answers "if nothing changes, what breaks?", which
is the question a warning is for.

This module holds no I/O. Network and fleet arrive as plain dicts, so the whole
projection is unit-testable without Redis or a filesystem.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import railsim.kinematics as kin
from railsim.topology import Leg, Topology

DEFAULT_HORIZON_S = 30 * 60
DEFAULT_HEADWAY_S = 120
MIN_PROJECTION_SPEED_KMH = 5.0
FALLBACK_MAX_SPEED_KMH = 130.0

MAX_REGULATION_SECONDS = int(os.getenv("MAX_REGULATION_SECONDS", "300"))
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "900"))

SEVERITY_BANDS = ((180, "CRITICAL"), (420, "HIGH"), (900, "MEDIUM"))

#: Margin over the bare execution time before a remedy is considered safe to
#: recommend. Braking, points, and the controller reading the screen all cost
#: time that the arithmetic alone does not capture.
ACTION_MARGIN_S = 90.0


@dataclass
class Occupancy:
    resource_id: str
    t_in: float
    t_out: float
    start_km: float
    end_km: float
    entry_station_id: str
    single_line: bool
    resource_length_km: float
    line_speed_kmh: float
    is_loop: bool = False


@dataclass
class TrackedTrain:
    train_id: str
    telemetry: Dict[str, Any]
    legs: List[Leg]
    route: List[str]
    station_km: Dict[str, float] = field(default_factory=dict)

    @property
    def distance_km(self) -> float:
        return float(self.telemetry["route_progress_km"])

    @property
    def speed_kmh(self) -> float:
        return max(MIN_PROJECTION_SPEED_KMH, float(self.telemetry["speed_kmh"]))

    @property
    def length_km(self) -> float:
        return kin.profile_for(self.telemetry["train_type"]).train_length_m / 1000.0

    @property
    def priority(self) -> float:
        return float(self.telemetry["priority_weight"])

    @property
    def in_loop_id(self) -> Optional[str]:
        value = self.telemetry.get("in_loop_id")
        return str(value) if value else None
    
    @property
    def standing_on_main(self) -> bool:
        return bool(self.telemetry.get("standing_on_main", False))

    @property
    def hold_station_id(self) -> Optional[str]:
        value = self.telemetry.get("hold_station_id")
        return str(value) if value else None

    @property
    def hold_loop_id(self) -> Optional[str]:
        value = self.telemetry.get("hold_loop_id")
        return str(value) if value else None

    @property
    def hold_until_train_id(self) -> Optional[str]:
        value = self.telemetry.get("hold_until_train_id")
        return str(value) if value else None

    @property
    def hold_expires_in_s(self) -> Optional[float]:
        value = self.telemetry.get("hold_expires_in_s")
        return None if value is None else float(value)

    @property
    def blocked(self) -> bool:
        """Standing at a red on the RUNNING LINE: not going anywhere, and the
        release time is not ours to guess.

        `schedule_status == HELD` is deliberately not a trigger any more. It is
        ambiguous across three different situations -- approaching a hold,
        standing in a loop, standing at a red -- and only the third one is a
        blockage of the running line. A train in a loop is handled by
        `project()` from `in_loop_id`, with a BOUNDED occupancy.
        """
        return (
            self.telemetry.get("signal_aspect") == "RED"
            and float(self.telemetry["speed_kmh"]) < 1.0
            and self.in_loop_id is None
        )


class ConflictDetector:
    def __init__(
        self,
        network: Dict[str, Any],
        fleet: Dict[str, Dict[str, Any]],
        horizon_seconds: int = DEFAULT_HORIZON_S,
        headway_seconds: int = DEFAULT_HEADWAY_S,
    ) -> None:
        self.topology = Topology(network)
        self.horizon_seconds = horizon_seconds
        self.headway_seconds = headway_seconds

        self._max_speed: Dict[str, float] = {}
        self._target_speed: Dict[str, float] = {}
        self._routes: Dict[str, List[str]] = {}
        self._legs: Dict[str, List[Leg]] = {}
        self._station_km: Dict[str, Dict[str, float]] = {}

        for entry in fleet.values():
            self.register_train(entry)

        self.trains: Dict[str, TrackedTrain] = {}
        self._projection_cache: Dict[str, List[Occupancy]] = {}
        self._projecting: set = set()

    # -- fleet registry ----------------------------------------------------

    def register_train(self, entry: Dict[str, Any]) -> bool:
        """Precompute legs and station marks for one train. Idempotent.

        Returns False if the route cannot be laid on this topology, which is a
        genuine misalignment between fleet and network rather than a transient.
        """
        train_id = str(entry["train_id"])
        route = list(entry["route"])

        try:
            legs = self.topology.build_legs(route)
        except ValueError as exc:
            print(f"[detector] rejecting {train_id}: {exc}")
            return False

        marks = {
            (leg.link.from_id if leg.direction == "DOWN" else leg.link.to_id):
            leg.route_start_km
            for leg in legs
        }
        marks[route[-1]] = self.topology.route_length_km(legs)

        self._routes[train_id] = route
        self._legs[train_id] = legs
        self._station_km[train_id] = marks
        self._max_speed[train_id] = float(
            entry.get("max_speed_kmh", FALLBACK_MAX_SPEED_KMH)
        )
        self._target_speed[train_id] = float(
            entry.get("scheduled_speed_kmh")
            or entry.get("max_speed_kmh", FALLBACK_MAX_SPEED_KMH)
        )
        return True

    def knows(self, train_id: str) -> bool:
        return str(train_id) in self._legs

    @property
    def fleet_size(self) -> int:
        return len(self._legs)

    # -- ingest ------------------------------------------------------------

    def ingest(self, event: Dict[str, Any]) -> bool:
        """Absorb one telemetry packet. False means the train is unregistered."""
        if event.get("event_type") != "TRAIN_TELEMETRY":
            return True
        train_id = str(event["train_id"])
        if train_id not in self._legs:
            return False
        if "route_progress_km" not in event:
            # Without route progress the detector would have to invert lat/lng
            # back onto the topology, which is lossy at junctions. Fail loudly.
            raise KeyError(
                f"Telemetry for {train_id} lacks route_progress_km; the detector "
                "cannot project occupancy without it."
            )
        self.trains[train_id] = TrackedTrain(
            train_id=train_id,
            telemetry=event,
            legs=self._legs[train_id],
            route=self._routes[train_id],
            station_km=self._station_km[train_id],
        )
        self._projection_cache.clear()
        return True

    # -- projection --------------------------------------------------------

    def _time_to_cover(
        self, distance_km: float, entry_speed_kmh: float, target_kmh: float, accel_ms2: float
    ) -> float:
        """Seconds to run `distance_km`, accelerating from entry toward target.

        Instantaneous speed is the wrong basis for a 30-minute projection. A
        freight pulling away from a loop at 5 km/h is not going to take eight
        hours to clear a 40 km section -- it will reach 60 km/h in about two
        minutes.

        Substituting SCHEDULED speed swaps one error for a worse one: it
        projects a train genuinely standing at a red as though it were moving,
        turning a real blockage into a MISSED conflict. So this accelerates from
        where the train actually is, and `blocked` handles the standing case
        separately.
        """
        return kin.traverse_seconds_accelerating(
            distance_km * 1000.0,
            kin.kmh_to_ms(entry_speed_kmh),
            kin.kmh_to_ms(target_kmh),
            accel_ms2,
        )

    def _window(self, position, t_in: float, t_out: float) -> Occupancy:
        return Occupancy(
            resource_id=position.resource_id,
            t_in=t_in,
            t_out=t_out,
            start_km=position.resource_start_km,
            end_km=position.resource_end_km,
            entry_station_id=position.entry_station_id,
            single_line=position.single_line,
            resource_length_km=position.resource_end_km - position.resource_start_km,
            line_speed_kmh=position.link_max_speed_kmh,
            is_loop=False,
        )

    def _loop_window(self, train: TrackedTrain, t_in: float, t_out: float) -> Occupancy:
        loop_id = train.in_loop_id or train.hold_loop_id or f"LOOP-{train.train_id}"
        station_id = train.hold_station_id or ""
        km = train.station_km.get(station_id, train.distance_km)
        return Occupancy(
            resource_id=loop_id,
            t_in=t_in,
            t_out=min(t_out, float(self.horizon_seconds)),
            start_km=km,
            end_km=km,
            entry_station_id=station_id,
            single_line=False,
            resource_length_km=0.0,
            line_speed_kmh=0.0,
            is_loop=True,
        )

    def _project_cached(self, train: TrackedTrain) -> Optional[List[Occupancy]]:
        """Projection of another train, memoised for this detection pass.

        Returns None if `train` is already being projected further up the stack.
        A holds b, b holds a is a controller error rather than a physical state,
        but it must not become a stack overflow in the engine.
        """
        if train.train_id in self._projecting:
            return None
        cached = self._projection_cache.get(train.train_id)
        if cached is not None:
            return cached
        self._projecting.add(train.train_id)
        try:
            windows = self.project(train)
        finally:
            self._projecting.discard(train.train_id)
        self._projection_cache[train.train_id] = windows
        return windows

    def _exit_station_for_hold(self, train: TrackedTrain) -> Optional[str]:
        """The far end of the resource this train is being held out of."""
        station_id = train.hold_station_id
        if station_id is None:
            return None
        try:
            index = train.route.index(station_id)
        except ValueError:
            return None
        if index + 1 >= len(train.route):
            return None
        return train.route[index + 1]
    
    def _blocked_release_seconds(self, train: TrackedTrain) -> float:
        """When the resource ahead of a train standing at a red clears.

        Occupancy is head AND tail. A 700 m rake whose head has crossed into the
        next block still fouls the one behind it, so resolving only the head
        position misses the blocker for the whole straddle -- and a miss here
        silently reinstates the truncated projection this method exists to
        prevent. Both ends are tested, each in the blocker's own route
        coordinates, so opposing moves compare correctly too.
        """
        route_length = self.topology.route_length_km(train.legs)
        ahead = self.topology.resolve(
            train.legs, min(train.distance_km + 0.2, route_length - 1e-6)
        )

        release = 0.0
        found = False
        for other in self.trains.values():
            if other.train_id == train.train_id or other.in_loop_id is not None:
                continue
            other_length = self.topology.route_length_km(other.legs)
            head_km = min(other.distance_km, other_length - 1e-6)
            for probe_km in (head_km, max(0.0, other.distance_km - other.length_km)):
                seen = self.topology.resolve(
                    other.legs, min(max(probe_km, 0.0), other_length - 1e-6)
                )
                if seen.resource_id != ahead.resource_id:
                    continue
                remaining_km = (
                    seen.resource_end_km + other.length_km - other.distance_km
                )
                if remaining_km <= 0.0:
                    break
                position = self.topology.resolve(other.legs, head_km)
                target_kmh = min(
                    self._target_speed.get(other.train_id, FALLBACK_MAX_SPEED_KMH),
                    position.link_max_speed_kmh,
                )
                profile = kin.profile_for(other.telemetry["train_type"])
                clearance = self._time_to_cover(
                    remaining_km, other.speed_kmh, target_kmh, profile.accel_ms2
                )
                if math.isfinite(clearance):
                    release = max(release, clearance)
                    found = True
                break

        if not found:
            occupants = ",".join(
                f"{o.train_id}:"
                f"{self.topology.resolve(o.legs, min(o.distance_km, self.topology.route_length_km(o.legs) - 1e-6)).resource_id}"
                for o in self.trains.values()
                if o.train_id != train.train_id
            )
            return float(self.horizon_seconds)

        return min(float(self.horizon_seconds), release + self.headway_seconds)

    def _release_seconds(self, train: TrackedTrain) -> float:
        """Seconds from now until this train can actually leave the loop.

        The simulator gates a pull-out on TWO conditions, not one:

            _hold_discharged     the train being given precedence is past the
                                 holding station
            movement authority   the resource ahead is not held by anyone

        Mirroring only the first is what makes a held train look like it pulls
        out into an occupied single line -- the detector then reports a conflict
        that can never clear, which is the original symptom in a new costume. So
        the release time is the moment the releasing train is clear of the WHOLE
        contested resource, plus one headway.

        The resource is bounded by the holding station and the next station on
        the held train's route. Which of those two the releasing train passes
        LAST is its exit, and that is direction-agnostic: for an overtake it is
        the far station, for a crossing it is the holding station itself.
        """
        expiry = train.hold_expires_in_s
        ceiling = (
            float(self.horizon_seconds) if expiry is None
            else min(float(expiry), float(self.horizon_seconds))
        )

        other_id = train.hold_until_train_id
        if other_id is None or train.hold_station_id is None:
            return ceiling

        other = self.trains.get(other_id)
        if other is None:
            return ceiling

        hold_km = train.station_km.get(train.hold_station_id)
        if hold_km is None:
            return ceiling
        contested = self.topology.resolve(train.legs, hold_km + 1e-4).resource_id

        # Read the clearance off the releasing train's OWN projection rather
        # than re-deriving it. A second kinematic estimate of the same quantity
        # disagrees with the first wherever line speed changes at a resource
        # boundary, and a 30-second disagreement is enough to leave the conflict
        # standing after the hold has resolved it -- the exact symptom this is
        # meant to remove. One number, one derivation.
        windows = self._project_cached(other)
        if windows is None:
            return ceiling

        for window in windows:
            if window.resource_id == contested:
                return max(0.0, min(ceiling, window.t_out + self.headway_seconds))

        marks = self._station_km.get(other.train_id, {})
        boundaries = [marks.get(train.hold_station_id)]
        exit_station = self._exit_station_for_hold(train)
        if exit_station is not None:
            boundaries.append(marks.get(exit_station))
        known = [km for km in boundaries if km is not None]
        if known and other.distance_km > max(known):
            return 0.0
        return ceiling

    def project(self, train: TrackedTrain) -> List[Occupancy]:
        """Ordered occupancy windows for the next `horizon_seconds`.

        Three regimes, and the whole point is that they are distinguished:

          standing in a loop   claims the LOOP for a BOUNDED interval, releases
                               the running line, then resumes from a stand.
          hold accepted        runs normally to the holding station, takes the
                               loop for the hold, then resumes from a stand.
          otherwise            the ordinary walk, unchanged.

        Pinning the loop occupancy at the horizon would resolve the conflict by
        blinding the projection to the pull-out, which on a single line is the
        most dangerous movement in the scenario.
        """
        route_length = self.topology.route_length_km(train.legs)
        windows: List[Occupancy] = []
        profile = kin.profile_for(train.telemetry["train_type"])
        train_target_kmh = self._target_speed.get(
            train.train_id, FALLBACK_MAX_SPEED_KMH
        )

        distance = train.distance_km
        entry_speed = train.speed_kmh
        elapsed = 0.0
        blocked_until: Optional[float] = None

        hold_km: Optional[float] = None
        if train.hold_station_id is not None and train.in_loop_id is None:
            candidate = train.station_km.get(train.hold_station_id)
            if candidate is not None and candidate >= train.distance_km - 0.05:
                hold_km = candidate

        if train.in_loop_id is not None:
            release = self._release_seconds(train)
            windows.append(self._loop_window(train, 0.0, release))
            if release >= self.horizon_seconds:
                return windows
            elapsed = release
            entry_speed = 0.0
            station_km = train.station_km.get(train.hold_station_id or "")
            if station_km is not None:
                distance = max(distance, station_km)

        elif train.blocked:
            blocked_until = self._blocked_release_seconds(train)
            if blocked_until >= self.horizon_seconds:
                position = self.topology.resolve(
                    train.legs, min(distance, route_length - 1e-6)
                )
                return [self._window(position, 0.0, float(self.horizon_seconds))]
            elapsed = blocked_until
            entry_speed = 0.0

        guard = 0
        while distance < route_length and guard < 500:
            guard += 1
            position = self.topology.resolve(
                train.legs, min(distance, route_length - 1e-6)
            )
            target_kmh = min(train_target_kmh, position.link_max_speed_kmh)

            if hold_km is not None and position.resource_start_km >= hold_km - 1e-6:
                approach_km = max(0.0, hold_km - distance)
                arrival = elapsed + self._time_to_cover(
                    approach_km, entry_speed, target_kmh, profile.accel_ms2
                )
                if not math.isfinite(arrival) or arrival > self.horizon_seconds:
                    break
                release = arrival + self._release_seconds(train)
                if train.standing_on_main:
                    if windows:
                        windows[-1].t_out = min(
                            release, float(self.horizon_seconds)
                        )
                    else:
                        here = self.topology.resolve(
                            train.legs, min(distance, route_length - 1e-6)
                        )
                        windows.append(
                            self._window(
                                here, 0.0, min(release, float(self.horizon_seconds))
                            )
                        )
                else:
                    windows.append(self._loop_window(train, arrival, release))
                if release >= self.horizon_seconds:
                    break
                elapsed = release
                entry_speed = 0.0
                distance = hold_km
                hold_km = None
                continue

            span_km = (
                position.resource_end_km
                + train.length_km
                - max(distance, position.resource_start_km)
            )
            traversal = self._time_to_cover(
                span_km, entry_speed, target_kmh, profile.accel_ms2
            )
            t_in = 0.0 if (blocked_until is not None and guard == 1) else elapsed
            t_out = elapsed + traversal
            if t_in > self.horizon_seconds:
                break
            if not math.isfinite(t_out):
                break

            windows.append(self._window(position, t_in, t_out))
            entry_ms = kin.kmh_to_ms(entry_speed)
            reached_ms = math.sqrt(
                max(0.0, entry_ms * entry_ms + 2.0 * profile.accel_ms2 * span_km * 1000.0)
            )
            entry_speed = min(target_kmh, reached_ms * 3.6)
            elapsed = t_out
            distance = position.resource_end_km + 1e-4

        return windows

    # -- detection ---------------------------------------------------------

    def detect(self) -> List[Dict[str, Any]]:
        """All predicted resource conflicts, most imminent first."""
        self._projection_cache.clear()
        projections = {
            tid: self._project_cached(t) or [] for tid, t in self.trains.items()
        }
        conflicts: List[Dict[str, Any]] = []

        ids = sorted(self.trains)
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1:]:
                conflicts.extend(
                    self._all_clashes(
                        self.trains[a_id], projections[a_id],
                        self.trains[b_id], projections[b_id],
                    )
                )

        conflicts.sort(key=lambda c: c["predicted_time_to_conflict_seconds"])
        return conflicts

    def _all_clashes(
        self,
        a: TrackedTrain, a_windows: Sequence[Occupancy],
        b: TrackedTrain, b_windows: Sequence[Occupancy],
    ) -> List[Dict[str, Any]]:
        """Every resource this pair contends for, not just the nearest.

        Returning only the first clash hides a pair's later contentions until
        its earlier one resolves, at which point the hidden one enters its
        group's clock at whatever value it has already counted down to. The
        group's min() then falls by hundreds of seconds with no train having
        moved unusually. A pair that will contend for two resources contends for
        both now, and both count down continuously.

        Loop windows stay excluded: loop capacity is a NoOverlap in the CP model
        and raising it here gives the controller a conflict with no action.
        """
        by_resource: Dict[str, Occupancy] = {
            w.resource_id: w for w in b_windows if not w.is_loop
        }

        clashes: List[Dict[str, Any]] = []
        for window_a in a_windows:
            if window_a.is_loop:
                continue
            window_b = by_resource.get(window_a.resource_id)
            if window_b is None:
                continue
            if (
                window_a.t_in < window_b.t_out + self.headway_seconds
                and window_b.t_in < window_a.t_out + self.headway_seconds
            ):
                contested_at = max(window_a.t_in, window_b.t_in)
                clashes.append(
                    self._package(a, b, window_a, window_b, contested_at)
                )
        return clashes

    def _package(
        self,
        a: TrackedTrain, b: TrackedTrain,
        window_a: Occupancy, window_b: Occupancy,
        contested_at: float,
    ) -> Dict[str, Any]:
        severity = "LOW"
        for threshold, label in SEVERITY_BANDS:
            if contested_at <= threshold:
                severity = label
                break

        loser, winner = (b, a) if a.priority >= b.priority else (a, b)

        # Clearance uses the projected traversal, not distance/instantaneous
        # speed -- otherwise a train pulling away from a stand reports a 480
        # minute cascading impact.
        clearance_s = max(0.0, window_b.t_out - window_b.t_in)

        cause = (
            f"{a.telemetry['train_name']} and {b.telemetry['train_name']} both "
            f"require {window_a.resource_id}"
        )
        if window_a.single_line:
            cause += (
                f" -- a {window_a.resource_length_km:.0f} km single line that admits "
                f"one train at a time; the second waits the full "
                f"{clearance_s / 60:.0f} min clearance."
            )
        else:
            cause += " on the same running line."

        return {
            "resource_id": window_a.resource_id,
            "single_line": window_a.single_line,
            "resource_length_km": window_a.resource_length_km,
            "line_speed_kmh": window_a.line_speed_kmh,
            "entry_station_a": window_a.entry_station_id,
            "entry_station_b": window_b.entry_station_id,
            "predicted_time_to_conflict_seconds": int(round(contested_at)),
            "severity": severity,
            "conflicting_train_ids": [a.train_id, b.train_id],
            "root_cause": cause,
            "estimated_cascading_impact_minutes": int(round(clearance_s / 60)),
            "actionable": self._actionable(loser, window_b if loser is b else window_a),
            "_windows": {a.train_id: window_a, b.train_id: window_b},
            "_likely_loser": loser.train_id,
            "_likely_winner": winner.train_id,
        }

    def _actionable(self, loser: TrackedTrain, window: Occupancy) -> bool:
        """Can the remedy still be executed, or is this now just a notification?

        Severity is the WRONG trigger for acting. A 40 km single line escalates
        to CRITICAL only once a train is nearly on top of it -- by which point
        holding anyone is pointless, because the train you would hold is already
        inside the section. What matters is whether the train that must give way
        can still reach its holding point before it commits to the resource.
        """
        distance_km = window.start_km - loser.distance_km
        if distance_km <= 0:
            return False  # already committed; nothing to hold it with
        return distance_km / loser.speed_kmh * 3600.0 > ACTION_MARGIN_S

    def detect_grouped(self) -> List[Dict[str, Any]]:
        """Conflicts grouped by CONTESTED RESOURCE rather than by pair.

        Pairwise detection is how the freight gets missed. On a 40 km single
        line the freight conflicts with the down express AND with the up
        express; solving either pair alone leaves the freight in the section and
        nothing improves. Precedence over a shared resource is a joint decision,
        and the solver already enumerates orderings for up to five trains -- it
        just has to be handed all of them at once.
        """
        pairs = self.detect()
        groups: Dict[str, Dict[str, Any]] = {}

        for conflict in pairs:
            resource = conflict["resource_id"]
            group = groups.get(resource)
            if group is None:
                groups[resource] = {
                    **conflict,
                    "conflicting_train_ids": list(conflict["conflicting_train_ids"]),
                    "_windows": dict(conflict["_windows"]),
                }
                continue

            for train_id, window in conflict["_windows"].items():
                if train_id not in group["_windows"]:
                    group["conflicting_train_ids"].append(train_id)
                    group["_windows"][train_id] = window
            group["predicted_time_to_conflict_seconds"] = min(
                group["predicted_time_to_conflict_seconds"],
                conflict["predicted_time_to_conflict_seconds"],
            )
            group["actionable"] = group["actionable"] or conflict["actionable"]
            group["estimated_cascading_impact_minutes"] = max(
                group["estimated_cascading_impact_minutes"],
                conflict["estimated_cascading_impact_minutes"],
            )

        for group in groups.values():
            ids = group["conflicting_train_ids"]
            if len(ids) > 2:
                names = ", ".join(self.trains[i].telemetry["train_name"] for i in ids)
                extent = (
                    f"a {group['resource_length_km']:.0f} km single line"
                    if group["single_line"] else "the same running line"
                )
                group["root_cause"] = (
                    f"{len(ids)} trains ({names}) contend for {group['resource_id']}, "
                    f"{extent}. Precedence must be decided for all of them together."
                )

        return sorted(
            groups.values(),
            key=lambda g: (
                not g["actionable"],
                -g["estimated_cascading_impact_minutes"] * len(g["conflicting_train_ids"]),
                g["predicted_time_to_conflict_seconds"],
            ),
        )

    # -- packaging for the optimiser ---------------------------------------

    def optimiser_inputs(
        self, conflict: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Build the exact (trains_in_conflict, track_topology) pair the solver wants.

        Two things here are the fix for block-level thinking:

          * `length_m` is the length of the CONTESTED RESOURCE. On a single line
            that is the whole 40 km section, so the solver's occupancy term
            (length + rake) / speed becomes the full section clearance time on
            its own. Inflating `headway_seconds` to fake this would give the
            right answer for the wrong reason and break the moment speeds change.

          * each train carries the loop AT ITS OWN ENTRY STATION. A loop that
            fits the rake but sits on the far side of the section is useless;
            picking one by length alone produces plans that cannot be executed.
        """
        trains_in_conflict = []
        for train_id in conflict["conflicting_train_ids"]:
            train = self.trains[train_id]
            window = conflict["_windows"][train_id]
            length_m = train.length_km * 1000.0
            loop = self.topology.loop_at(window.entry_station_id, length_m)

            trains_in_conflict.append(
                {
                    "train_id": train_id,
                    "train_name": train.telemetry["train_name"],
                    "train_type": train.telemetry["train_type"],
                    "current_speed": train.speed_kmh,
                    "target_speed_kmh": self._target_speed.get(
                        train_id, FALLBACK_MAX_SPEED_KMH
                    ),
                    "max_speed_kmh": self._max_speed.get(
                        train_id, FALLBACK_MAX_SPEED_KMH
                    ),
                    "distance_to_bottleneck": max(
                        0.0, (window.start_km - train.distance_km) * 1000.0
                    ),
                    "priority_weight": train.priority,
                    "train_length_m": length_m,
                    "existing_delay_seconds": int(train.telemetry.get("delay_seconds", 0)),
                    "hold_station_id": window.entry_station_id,
                    "hold_loop_id": loop.id if loop else None,
                    "hold_loop_length_m": loop.usable_length_m if loop else 0.0,
                }
            )

        track_topology = {
            "block_id": conflict["resource_id"],
            "resource_id": conflict["resource_id"],
            "junction_id": conflict["entry_station_a"],
            "single_line": conflict["single_line"],
            "length_m": conflict["resource_length_km"] * 1000.0,
            "line_speed_kmh": conflict["line_speed_kmh"],
            "headway_seconds": self.headway_seconds,
            "max_regulation_seconds": MAX_REGULATION_SECONDS,
            "max_hold_seconds": MAX_HOLD_SECONDS,
        }
        return trains_in_conflict, track_topology