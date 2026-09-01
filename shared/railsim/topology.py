"""Static railway topology, addressed by interlocking RESOURCE rather than by block.

The central idea, and the thing the previous version got wrong:

  On a DOUBLE line the exclusive resource is (running line, block). Two trains
  in opposite directions are on physically different rails and can never
  conflict, so keying occupancy on direction is correct.

  On a SINGLE line there is one rail. Absolute block working locks the ENTIRE
  station-to-station section -- from the last loop before it to the first loop
  after it -- for one train in one direction. Slicing it into 4 km blocks and
  keying them by direction lets two opposing trains occupy the same rail with
  different keys, i.e. drive straight through each other.

`resource_id` is therefore the unit of exclusion, and `resolve()` returns both
it and the route distance at which it begins, so a train can be stopped BEFORE
entering an occupied resource rather than after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DOWN = "DOWN"
UP = "UP"


@dataclass(frozen=True)
class Loop:
    id: str
    station_id: str
    usable_length_m: float


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lng: float
    loops: Tuple[Loop, ...] = ()

    def longest_loop_m(self) -> float:
        return max((loop.usable_length_m for loop in self.loops), default=0.0)

    def loop_for(self, train_length_m: float) -> Optional[Loop]:
        fitting = [loop for loop in self.loops if loop.usable_length_m >= train_length_m]
        return min(fitting, key=lambda loop: loop.usable_length_m) if fitting else None


@dataclass(frozen=True)
class Block:
    id: str
    start_km: float
    end_km: float


@dataclass
class Link:
    index: int
    from_id: str
    to_id: str
    distance_km: float
    max_speed_kmh: float
    single_line: bool
    blocks: List[Block] = field(default_factory=list)

    @property
    def section_id(self) -> str:
        return f"SEC-{self.from_id}-{self.to_id}"


@dataclass(frozen=True)
class Leg:
    link: Link
    direction: str
    route_start_km: float

    @property
    def route_end_km(self) -> float:
        return self.route_start_km + self.link.distance_km


@dataclass(frozen=True)
class Position:
    block_id: str
    track_id: str
    section_id: str
    #: The unit of mutual exclusion. Compare THIS between trains, never block_id.
    resource_id: str
    #: Route distance at which the current resource begins. A train denied entry
    #: must be stopped here, at the protecting signal, not somewhere inside.
    resource_start_km: float
    resource_end_km: float
    single_line: bool
    lat: float
    lng: float
    link_max_speed_kmh: float
    entry_station_id: str
    next_station_id: str
    km_to_next_station: float
    direction: str
    at_terminus: bool


class Topology:
    def __init__(self, seed: dict):
        self.section_id: str = seed["meta"]["section_id"]
        self.block_length_km: float = float(seed["meta"].get("block_length_km", 4.0))

        self.stations: Dict[str, Station] = {}
        for raw in seed["stations"]:
            loops = tuple(
                Loop(
                    id=loop["id"],
                    station_id=raw["id"],
                    usable_length_m=float(loop["usable_length_m"]),
                )
                for loop in raw.get("loops", [])
            )
            self.stations[raw["id"]] = Station(
                id=raw["id"], name=raw["name"],
                lat=float(raw["lat"]), lng=float(raw["lng"]), loops=loops,
            )

        self.links: List[Link] = []
        self._link_by_pair: Dict[Tuple[str, str], Link] = {}

        counter = 100
        for index, raw in enumerate(seed["links"]):
            link = Link(
                index=index,
                from_id=raw["from"],
                to_id=raw["to"],
                distance_km=float(raw["distance_km"]),
                max_speed_kmh=float(raw["max_speed_kmh"]),
                single_line=bool(raw.get("single_line", False)),
            )
            counter = self._partition(link, counter)
            self.links.append(link)
            self._link_by_pair[(link.from_id, link.to_id)] = link
            self._link_by_pair[(link.to_id, link.from_id)] = link

    def _partition(self, link: Link, counter: int) -> int:
        """Blocks exist on every link for DISPLAY. On a single line they are not
        the unit of exclusion -- the section is. See resolve()."""
        count = max(1, round(link.distance_km / self.block_length_km))
        span = link.distance_km / count
        for i in range(count):
            start = i * span
            end = link.distance_km if i == count - 1 else (i + 1) * span
            link.blocks.append(Block(id=f"BLK-{counter + i}", start_km=start, end_km=end))
        return counter + count

    def link_between(self, a: str, b: str) -> Optional[Link]:
        return self._link_by_pair.get((a, b))

    def build_legs(self, route: Sequence[str]) -> List[Leg]:
        legs: List[Leg] = []
        cumulative = 0.0
        for a, b in zip(route, route[1:]):
            link = self._link_by_pair.get((a, b))
            if link is None:
                raise ValueError(f"No link between {a} and {b}; route is not connected.")
            direction = DOWN if (link.from_id, link.to_id) == (a, b) else UP
            legs.append(Leg(link=link, direction=direction, route_start_km=cumulative))
            cumulative += link.distance_km
        return legs

    @staticmethod
    def route_length_km(legs: Sequence[Leg]) -> float:
        return legs[-1].route_end_km if legs else 0.0

    def leg_at(self, legs: Sequence[Leg], distance_km: float) -> Leg:
        clamped = min(max(0.0, distance_km), self.route_length_km(legs))
        for leg in legs:
            if clamped < leg.route_end_km:
                return leg
        return legs[-1]

    def resolve(self, legs: Sequence[Leg], distance_km: float) -> Position:
        total = self.route_length_km(legs)
        at_terminus = distance_km >= total
        clamped = min(max(0.0, distance_km), total)

        leg = self.leg_at(legs, clamped)
        offset = clamped - leg.route_start_km
        link = leg.link

        canonical_offset = offset if leg.direction == DOWN else link.distance_km - offset
        block = link.blocks[-1]
        for candidate in link.blocks:
            if canonical_offset < candidate.end_km:
                block = candidate
                break

        origin_id, target_id = (
            (link.from_id, link.to_id) if leg.direction == DOWN else (link.to_id, link.from_id)
        )
        origin = self.stations[origin_id]
        target = self.stations[target_id]
        fraction = 0.0 if link.distance_km == 0 else offset / link.distance_km

        if link.single_line:
            # One rail. The whole section is the token; direction is irrelevant
            # to exclusion, which is exactly the point.
            resource_id = link.section_id
            resource_start_km = leg.route_start_km
            resource_end_km = leg.route_end_km
            track_id = f"TRK-{link.section_id}-SINGLE"
            block_id = f"{block.id}S"
        else:
            track_id = f"TRK-{leg.direction}-MAIN"
            block_id = f"{block.id}{'D' if leg.direction == DOWN else 'U'}"
            resource_id = f"{track_id}|{block_id}"
            # Map the block's canonical span into route coordinates for this leg.
            if leg.direction == DOWN:
                resource_start_km = leg.route_start_km + block.start_km
                resource_end_km = leg.route_start_km + block.end_km
            else:
                resource_start_km = leg.route_start_km + (link.distance_km - block.end_km)
                resource_end_km = leg.route_start_km + (link.distance_km - block.start_km)

        return Position(
            block_id=block_id,
            track_id=track_id,
            section_id=self.section_id,
            resource_id=resource_id,
            resource_start_km=resource_start_km,
            resource_end_km=resource_end_km,
            single_line=link.single_line,
            lat=origin.lat + (target.lat - origin.lat) * fraction,
            lng=origin.lng + (target.lng - origin.lng) * fraction,
            link_max_speed_kmh=link.max_speed_kmh,
            entry_station_id=origin_id,
            next_station_id=target_id,
            km_to_next_station=max(0.0, link.distance_km - offset),
            direction=leg.direction,
            at_terminus=at_terminus,
        )

    def loop_at(self, station_id: str, train_length_m: float) -> Optional[Loop]:
        station = self.stations.get(station_id)
        return station.loop_for(train_length_m) if station else None

    def station_before(
        self,
        legs: Sequence[Leg],
        from_km: float,
        to_km: float,
    ) -> Optional[str]:
        """Last station in [from_km, to_km] along this route, or None.

        from_km is where the train is now, to_km is where the contested
        resource begins. A station outside that span is either astern of the
        train or beyond the thing it is being held short of. None means a
        stand is genuinely impossible on this approach.
        """
        if not legs or to_km < from_km:
            return None
        best: Optional[Tuple[float, str]] = None
        for leg in legs:
            origin_id, target_id = (
                (leg.link.from_id, leg.link.to_id)
                if leg.direction == DOWN
                else (leg.link.to_id, leg.link.from_id)
            )
            for km, station_id in (
                (leg.route_start_km, origin_id),
                (leg.route_end_km, target_id),
            ):
                if from_km <= km <= to_km and (best is None or km > best[0]):
                    best = (km, station_id)
        return best[1] if best else None

    def resource_length_km(self, legs: Sequence[Leg], distance_km: float) -> float:
        position = self.resolve(legs, distance_km)
        return position.resource_end_km - position.resource_start_km

    @classmethod
    def from_file(cls, path: str | Path) -> "Topology":
        network = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(network)