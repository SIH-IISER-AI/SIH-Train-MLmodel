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
"""

from __future__ import annotations

import json

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import railsim.kinematics as kin
from railsim.topology import Leg, Topology

DEFAULT_HORIZON_S = 30 * 60
DEFAULT_HEADWAY_S = 120
MIN_PROJECTION_SPEED_KMH = 5.0

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
    def blocked(self) -> bool:
        """Standing at a red or under a controller hold: not going anywhere."""
        return (
            self.telemetry.get("signal_aspect") == "RED"
            or self.telemetry.get("schedule_status") == "HELD"
        ) and float(self.telemetry["speed_kmh"]) < 1.0


class ConflictDetector:
    def __init__(
        self,
        network_path: str | Path,
        scenario_path: str | Path,
        horizon_seconds: int = DEFAULT_HORIZON_S,
        headway_seconds: int = DEFAULT_HEADWAY_S,
    ) -> None:
        self.topology = Topology.from_file(network_path)
        scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
        
        self.horizon_seconds = horizon_seconds
        self.headway_seconds = headway_seconds
        self._max_speed = {str(t["train_id"]): float(t["max_speed_kmh"]) for t in scenario["trains"]}

        self._routes: Dict[str, List[str]] = {
            str(t["train_id"]): list(t["route"]) for t in scenario["trains"]
        }
        self._legs: Dict[str, List[Leg]] = {
            train_id: self.topology.build_legs(route)
            for train_id, route in self._routes.items()
        }
        self._station_km: Dict[str, Dict[str, float]] = {}
        for train_id, legs in self._legs.items():
            marks = {
                (leg.link.from_id if leg.direction == "DOWN" else leg.link.to_id):
                leg.route_start_km
                for leg in legs
            }
            marks[self._routes[train_id][-1]] = self.topology.route_length_km(legs)
            self._station_km[train_id] = marks

        self.trains: Dict[str, TrackedTrain] = {}

    # -- ingest ------------------------------------------------------------

    def ingest(self, event: Dict[str, Any]) -> None:
        if event.get("event_type") != "TRAIN_TELEMETRY":
            return
        train_id = str(event["train_id"])
        if train_id not in self._legs:
            return
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
        if distance_km <= 0:
            return 0.0
        v0 = kin.kmh_to_ms(entry_speed_kmh)
        vt = kin.kmh_to_ms(max(target_kmh, entry_speed_kmh))
        distance_m = distance_km * 1000.0

        if vt <= v0 or accel_ms2 <= 0:
            return distance_m / max(v0, 0.5)

        accel_distance = (vt ** 2 - v0 ** 2) / (2.0 * accel_ms2)
        if distance_m <= accel_distance:
            return (-v0 + math.sqrt(v0 ** 2 + 2.0 * accel_ms2 * distance_m)) / accel_ms2
        return (vt - v0) / accel_ms2 + (distance_m - accel_distance) / vt

    def project(self, train: TrackedTrain) -> List[Occupancy]:
        """Ordered occupancy windows for the next `horizon_seconds`."""
        route_length = self.topology.route_length_km(train.legs)
        windows: List[Occupancy] = []
        profile = kin.profile_for(train.telemetry["train_type"])
        train_max_kmh = self._max_speed.get(train.train_id, 130.0)

        distance = train.distance_km
        entry_speed = train.speed_kmh
        elapsed = 0.0
        guard = 0
        while distance < route_length and guard < 500:
            guard += 1
            position = self.topology.resolve(train.legs, min(distance, route_length - 1e-6))
            target_kmh = min(train_max_kmh, position.link_max_speed_kmh)

            if train.blocked and guard == 1:
                # Standing at a red. It holds this resource for as far as we can
                # see and claims nothing beyond it -- exactly right: it IS the
                # blockage, and its release time is not ours to guess.
                windows.append(
                    Occupancy(
                        resource_id=position.resource_id,
                        t_in=0.0,
                        t_out=float(self.horizon_seconds),
                        start_km=position.resource_start_km,
                        end_km=position.resource_end_km,
                        entry_station_id=position.entry_station_id,
                        single_line=position.single_line,
                        resource_length_km=position.resource_end_km - position.resource_start_km,
                    )
                )
                break

            span_km = (
                position.resource_end_km
                + train.length_km
                - max(distance, position.resource_start_km)
            )
            t_in = elapsed
            t_out = t_in + self._time_to_cover(
                span_km, entry_speed, target_kmh, profile.accel_ms2
            )
            if t_in > self.horizon_seconds:
                break

            windows.append(
                Occupancy(
                    resource_id=position.resource_id,
                    t_in=t_in, t_out=t_out,
                    start_km=position.resource_start_km,
                    end_km=position.resource_end_km,
                    entry_station_id=position.entry_station_id,
                    single_line=position.single_line,
                    resource_length_km=position.resource_end_km - position.resource_start_km,
                )
            )
            # Step just past the end of this resource to land in the next one,
            # carrying the exit speed forward so acceleration compounds.
            entry_speed = target_kmh
            elapsed = t_out
            distance = position.resource_end_km + 1e-4

        return windows

    # -- detection ---------------------------------------------------------

    def detect(self) -> List[Dict[str, Any]]:
        """All predicted resource conflicts, most imminent first."""
        projections = {tid: self.project(t) for tid, t in self.trains.items()}
        conflicts: List[Dict[str, Any]] = []

        ids = sorted(self.trains)
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1:]:
                clash = self._first_clash(
                    self.trains[a_id], projections[a_id],
                    self.trains[b_id], projections[b_id],
                )
                if clash is not None:
                    conflicts.append(clash)

        conflicts.sort(key=lambda c: c["predicted_time_to_conflict_seconds"])
        return conflicts

    def _first_clash(
        self,
        a: TrackedTrain, a_windows: Sequence[Occupancy],
        b: TrackedTrain, b_windows: Sequence[Occupancy],
    ) -> Optional[Dict[str, Any]]:
        by_resource: Dict[str, Occupancy] = {w.resource_id: w for w in b_windows}

        for window_a in a_windows:
            window_b = by_resource.get(window_a.resource_id)
            if window_b is None:
                continue
            # Overlap with one headway of separation required on each side.
            if (
                window_a.t_in < window_b.t_out + self.headway_seconds
                and window_b.t_in < window_a.t_out + self.headway_seconds
            ):
                contested_at = max(window_a.t_in, window_b.t_in)
                return self._package(a, b, window_a, window_b, contested_at)
        return None

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
        clearance_s = min(window_b.t_out - window_b.t_in, float(self.horizon_seconds))

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
            "line_speed_kmh": 130.0,
            "headway_seconds": self.headway_seconds,
            "max_regulation_seconds": 300,
            "max_hold_seconds": 45 * 60,
        }
        return trains_in_conflict, track_topology