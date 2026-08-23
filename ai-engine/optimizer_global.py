"""ONE CP-SAT model over a lookahead window, replacing n! enumeration.

Day 5: scoping and skeleton only. The model is not built here yet -- days 6-7
add interval vars, precedence booleans and NoOverlap; days 8-9 add chaining.
optimize_global() deliberately raises until then, because a stub that silently
returns [] would look like "the global engine found nothing" for a week.

Why this exists, in one paragraph: optimizer.py solves each contention group
independently and fixes precedence by enumerating n! Python-level permutations.
That has two consequences measured in docs/baselines/ab-enumerate.csv. The cap
of 5 drops 4 of the 9 trains contending SEC-PWL-KSV, and the dropped ones are
exactly the freights and expresses that would be given way. And because the
solves cannot see each other, a per-solve anti-starvation cap of 900 s composed
into 8,533 s of accumulated delay on 40201 across ~35 individually-compliant
approvals, with policy_exceeded reading 0 throughout. Neither is a tuning
problem. Both follow from precedence living in a Python loop outside the
solver, and only a model that sees every (train, resource) pair at once can
state the constraint that was violated.

Read docs/GLOBAL_MODEL_SPEC.md before changing anything here. The five
decisions recorded there are load-bearing for days 6-14.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from optimizer import (
    DEFAULT_HEADWAY_SECONDS,
    DEFAULT_MAX_HOLD_SECONDS,
    priority_class,
)

#: Lookahead. Defaults to the detector's own horizon so the global model sees
#: exactly the conflicts the detector raises -- a wider window would solve for
#: contention the controller has not been warned about, and a narrower one
#: would leave a raised alert unaddressed. Measured interval counts at several
#: horizons are in docs/GLOBAL_MODEL_SPEC.md; 1800 s is inside the day-5 gate.
WINDOW_HORIZON_S = int(os.getenv("GLOBAL_HORIZON_S", "1800"))

#: A resource with one train in the window needs no ordering decision: no
#: NoOverlap, no precedence booleans, no interval var. Excluding them is not an
#: optimisation, it is the difference between 74 intervals and a model that
#: carries 28 resources of dead weight.
MIN_TRAINS_FOR_CONTENTION = 2


@dataclass(frozen=True)
class WindowInterval:
    """One (train, resource) pair inside the lookahead window.

    Constants come from detector.project(), NOT from optimizer._prepare().
    _prepare is single-resource by construction: it takes one block's topology
    dict and computes earliest_arrival_s as the unimpeded run from the train's
    CURRENT position to THAT block, at THAT block's line speed. Called once per
    (train, resource) it would ignore every speed restriction in between --
    measured at up to 207 s of drift before the day-5 parity fix. project()
    walks the route link by link; both use shared/railsim/kinematics, so after
    the fix they agree. tests/count_intervals.py asserts it.
    """

    train_id: str
    resource_id: str
    seq: int                    # position along this train's route in-window
    earliest_in_s: int          # unimpeded arrival at this resource's entry
    running_s: int              # head-in to tail-out, arriving in motion
    single_line: bool
    is_loop: bool
    entry_station_id: str
    resource_length_km: float
    line_speed_kmh: float
    train_type: str
    priority_weight: float

    @property
    def key(self) -> Tuple[str, str]:
        return (self.train_id, self.resource_id)


@dataclass
class WindowScope:
    """Everything days 6-9 need to build the model, and nothing else."""

    horizon_s: int
    intervals: List[WindowInterval] = field(default_factory=list)
    by_resource: Dict[str, List[WindowInterval]] = field(default_factory=dict)
    by_train: Dict[str, List[WindowInterval]] = field(default_factory=dict)
    contested: Dict[str, List[WindowInterval]] = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
        """The day-5 gate numbers. Ordered pairs, not unordered.

        Ordered is what gets declared: precedes[i,j,r] and precedes[j,i,r] are
        separate booleans tied by an == 1 constraint, because day 10 flips a
        NAMED boolean to build the counterfactual and "reverse the decision"
        has to mean one identifiable variable. CP-SAT presolves the redundant
        half away; the declaration count is what the gate measures.
        """
        ordered_pairs = sum(
            len(group) * (len(group) - 1) for group in self.contested.values()
        )
        return {
            "intervals": len(self.intervals),
            "resources": len(self.by_resource),
            "contested_resources": len(self.contested),
            "precedes_ordered": ordered_pairs,
            "trains": len(self.by_train),
            "largest_contention": max(
                (len(g) for g in self.contested.values()), default=0
            ),
        }


def scope_window(detector, horizon_s: Optional[int] = None) -> WindowScope:
    """Enumerate every (train, resource) pair whose projected t_in is in-window.

    Restores the detector's own horizon before returning. Mutating it and
    leaving it mutated would silently change every subsequent detect() in the
    same process, which is the kind of bug that shows up three files away.
    """
    horizon_s = int(horizon_s if horizon_s is not None else WINDOW_HORIZON_S)

    previous_horizon = detector.horizon_seconds
    detector.horizon_seconds = horizon_s
    detector._projection_cache.clear()
    try:
        scope = WindowScope(horizon_s=horizon_s)
        for train_id, tracked in detector.trains.items():
            telemetry = tracked.telemetry
            for seq, occupancy in enumerate(detector.project(tracked)):
                if occupancy.t_in >= horizon_s:
                    continue
                scope.intervals.append(
                    WindowInterval(
                        train_id=train_id,
                        resource_id=occupancy.resource_id,
                        seq=seq,
                        earliest_in_s=int(math.ceil(occupancy.t_in)),
                        running_s=max(1, int(occupancy.t_out - occupancy.t_in)),
                        single_line=bool(occupancy.single_line),
                        is_loop=bool(occupancy.is_loop),
                        entry_station_id=occupancy.entry_station_id,
                        resource_length_km=float(occupancy.resource_length_km),
                        line_speed_kmh=float(occupancy.line_speed_kmh),
                        train_type=str(telemetry.get("train_type", "EXPRESS")),
                        priority_weight=float(
                            telemetry.get("priority_weight", 6.0)
                        ),
                    )
                )
    finally:
        detector.horizon_seconds = previous_horizon
        detector._projection_cache.clear()

    for interval in scope.intervals:
        scope.by_resource.setdefault(interval.resource_id, []).append(interval)
        scope.by_train.setdefault(interval.train_id, []).append(interval)

    for resource_id, group in scope.by_resource.items():
        if len({i.train_id for i in group}) >= MIN_TRAINS_FOR_CONTENTION:
            scope.contested[resource_id] = group

    # Deterministic order everywhere downstream. by_resource comes from dict
    # insertion, which follows detector.trains, which follows the scenario
    # file -- stable, but only by accident. Sorting makes it stable on purpose,
    # and a CP-SAT model built in a different variable order is a different
    # search tree and can return a different optimum among ties.
    for group in scope.by_resource.values():
        group.sort(key=lambda i: (i.earliest_in_s, i.train_id))
    for group in scope.by_train.values():
        group.sort(key=lambda i: i.seq)

    return scope


def optimize_global(detector, horizon_s: Optional[int] = None):
    """Not built yet. Days 6-7 add intervals + precedence, 8-9 add chaining.

    Raising rather than returning [] is deliberate: an empty list is a valid
    answer from optimize_precedence and would be indistinguishable from "the
    global engine ran and found nothing" in every log and every CSV column for
    the next week.
    """
    raise NotImplementedError(
        "optimizer_global.optimize_global is a day-6 deliverable; "
        "ENGINE=global is not runnable yet. Use ENGINE=enumerate."
    )