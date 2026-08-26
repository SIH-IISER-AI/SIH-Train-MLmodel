"""ONE CP-SAT model over a lookahead window, replacing n! enumeration.

Days 8-11: route chaining, per-hop slack, cumulative hold, lexicographic
descent by IR priority class, the simulator's refusal rules as constraints,
and directive emission. Read docs/GLOBAL_MODEL_SPEC.md before changing this.

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

Route order without a second data structure
-------------------------------------------
The day-8 plan called for joining scope_window() into the model to supply
`seq`. It is not needed, and joining it would have been wrong.

_prepare().earliest_arrival_s IS the projection's t_in for that resource (the
day-5 parity fix passes window.t_in through), and a route is traversed in time
order. Sorting a train's intervals by earliest_arrival_s therefore recovers the
route order exactly; ties break on resource_id for determinism. The transit
between two of a train's resources is
`earliest_arrival_s[k+1] - earliest_arrival_s[k]`, which is correct whether the
resources are adjacent or ten blocks apart, and which is exactly tight at the
unimpeded baseline -- with every entry at its earliest, the chain constraint
holds with equality and no train acquires delay it was not given.

Chaining on `exit[k]` instead would use occupancy_running_s, and that constant
is the FULL block sweep from the train's CURRENT speed. For the resource a
train is already inside, the projection's remaining run is a partial traversal:
measured at tick 0 of scenario10, occupancy_running_s exceeds it by 278 s on
40201/BLK-114D, 250 s on 40208/BLK-128U, 223 s on 54402/BLK-145U, and by a
systematic +1 s everywhere else from ceil-versus-truncate rounding. Chained on
exit, every train acquires phantom delay before any dispatch decision is taken,
and the day-9 joint gate fails for a reason that is not an encoding error.

scope_window() therefore stays what it is: the sizing and contiguity instrument
used by tests/count_intervals.py. It is not on the model's path.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

from optimizer import (
    CLASS_LABELS,
    DEFAULT_HEADWAY_SECONDS,
    DEFAULT_MAX_HOLD_SECONDS,
    DELAY_TIER_BREAKS_S,
    DELAY_TIER_MULTIPLIERS,
    DIRECTIVE_RELEASE_TIMEOUT_S,
    PRIORITY_CLASS_COUNT,
    SOLVER_DETERMINISTIC_TIME,
    SOLVER_TIME_LIMIT_S,
    SOLVER_WORKERS,
    TIEBREAK_COEFFICIENT,
    _lower_first,
    _prepare,
    priority_class,
)
import railsim.kinematics as kin

#: Lookahead for scope_window(). Defaults to the detector's own horizon so the
#: sizing instrument measures the window the detector raises conflicts in. NOT
#: used by build_and_solve(), which is scoped by the payloads it is handed.
WINDOW_HORIZON_S = int(os.getenv("GLOBAL_HORIZON_S", "1800"))

#: A resource with one train in the window needs no ordering decision.
MIN_TRAINS_FOR_CONTENTION = 2

#: Decision 4, resolved. total_hold[t] = SUM over k of slack[t,k]: the time the
#: model chose to leave train t standing, counted ONCE at the resource where
#: the stand was imposed. There is no `forced` term to subtract -- once
#: precedence is a variable, "forced" is not a constant, and the per-solve
#: baseline that made cap_s[i] meaningful in _solve_order does not exist.
#:
#: Consequence, and the reason the default is SOFT: slack includes standing
#: because the section ahead is genuinely occupied. 12280 carries 8,813 s of
#: exactly that on one conflict. A 900 s hard cap on this quantity is infeasible
#: at tick 0 of the production scenario, and an infeasible model gives the
#: controller nothing at all.
#:
#:   soft  worst-case hold enters the lexicographic descent as the lowest tier.
#:         The model minimises the worst standing time subject to precedence.
#:   hard  total_hold[t] <= GLOBAL_HOLD_CAP_MULTIPLIER * max_hold_s, with the


#: Multiplier on max_hold_seconds for the hard backstop. 0 disables the bound
#: entirely. Set it from measurement -- tests/measure_hold.py prints the
#: observed distribution -- not from the per-conflict 900 s, which capped a
#: different quantity under a baseline that no longer exists.
GLOBAL_HOLD_CAP_MULTIPLIER = float(os.getenv("GLOBAL_HOLD_CAP_MULTIPLIER", "0"))

#: Standing time on one train beyond which the plan is FLAGGED to the
#: controller. Deliberately NOT derived from what the model chooses -- that is
#: the enumerate defect exactly: cap_s = forced_s + max_hold_s took its ceiling
#: from the quantity it was meant to constrain, so every approval was compliant
#: by construction and 40201 accumulated 8,533 s with policy_exceeded reading 0.
#:
#: 7200 s is two hours: the point at which a detention stops being regulation
#: and becomes a reportable event. It comes from operating practice, not from a
#: solve, which is the only reason it can bind. 0 disables the flag.
GLOBAL_STARVATION_THRESHOLD_S = int(
    os.getenv("GLOBAL_STARVATION_THRESHOLD_S", "7200")
)

#: A train is brought to a stand AT MOST this many times inside one window.
#: 1 is not a simplification, it is the operational rule: a controller who
#: stands the same train twice in half an hour has made two decisions where
#: one would have done, and the simulator carries one hold flag per train, so
#: a two-stop schedule cannot be expressed as directives at all. Relaxed by
#: solve_with_policy before the direction ban, since a plan that stops a train
#: twice is still a plan.
GLOBAL_MAX_STOPS = int(os.getenv("GLOBAL_MAX_STOPS", "1"))

#: Minimum approach distance for a stand to be EMITTED, in metres.
#:
#: An EMISSION filter, deliberately NOT a model constraint. The simulator
#: refuses a hold whose station lies behind the train, and for the resource a
#: train is already inside there is no such station ahead -- so emitting one is
#: pointless. But forbidding the model to CONSIDER the stand is a different and
#: worse thing: it changes the schedule for every other train on the resource.
#:
#: Measured on TRK-DOWN-MAIN|BLK-108D at scenario10 tick 0, pinned order
#: 12050 -> 20172: with the constraint in the model, 20172 enters at 344 s;
#: without it, 245 s, which is what _solve_order gives. 12050 is not stopped in
#: either solution and its exit is 125 s in both, so the 99 s is not the stand
#: being priced -- forbidding a decision perturbed a schedule that did not
#: depend on it.
#:
#: A stand the model prices but cannot emit is a costed decision the card
#: simply does not show. That is the cheaper error.
HOLD_MIN_APPROACH_M = 50.0

#: Wall-clock ceiling PER TIER, not for the descent as a whole. Dividing one
#: total across N tiers is what broke the descent: every Solve() re-runs full
#: presolve on the whole model, so a slice below ~0.3 s is consumed before
#: search starts. Measured: at 0.15 s per tier the descent completed 1 of 6 at
#: every total from 0.5 s to 2.0 s, and worst_hold (tier 4) never ran.
#:
#: Worst case is this times tiers -- about 3 s for nine trains, against the
#: 5.0 s ENUMERATION_BUDGET_S that enumerate cannot finish nine trains inside.
GLOBAL_TIER_BUDGET_S = float(os.getenv("GLOBAL_TIER_BUDGET_S", "0.6"))

#: Deterministic-time ceiling per solve. Wall-clock budgets make the answer a
#: function of machine load -- exactly the nondeterminism finding we hold
#: against enumerate above cap 6. Deterministic time is reproducible across
#: machines, so it is the primary limit; the wall clock stays as a safety net
#: at 3x, never as the thing that decides the plan. 0 disables.
GLOBAL_DET_BUDGET = float(os.getenv("GLOBAL_DET_BUDGET", "8.0"))

#: How far the backstop lifts before a plan is labelled policy_exceeded.
#: Mirrors optimize_precedence's 3x relaxation.
HOLD_RELAX_MULTIPLIER = 3.0

#: Day-12 experiment, default off. When on, a train's absorbable slack at
#: resource k>0 is capped by the run room on the PREVIOUS in-model hop rather
#: than by the whole approach from its current position. More conservative and
#: more correct under chaining, but it changes `stopped` flags relative to
#: enumerate and would fail the day-9 joint gate for a reason that is not an
#: encoding error. Turn it on deliberately, on day 12, with the delta measured.
CHAIN_LOCAL_ABSORB = bool(int(os.getenv("GLOBAL_LOCAL_ABSORB", "0")))


# ---------------------------------------------------------------------------
# Window scoping -- sizing instrument, not on the model path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowInterval:
    """One (train, resource) pair inside the lookahead window."""

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
    """Sizing view of the window. See tests/count_intervals.py."""

    horizon_s: int
    intervals: List[WindowInterval] = field(default_factory=list)
    by_resource: Dict[str, List[WindowInterval]] = field(default_factory=dict)
    by_train: Dict[str, List[WindowInterval]] = field(default_factory=dict)
    contested: Dict[str, List[WindowInterval]] = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
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
    same process.

    `seq` counts KEPT intervals, not raw projection index. t_in is monotonic so
    the filter only ever drops a suffix -- but a gapped seq would silently break
    the contiguity assertion in tests/count_intervals.py, which is the one thing
    this structure is now for.
    """
    horizon_s = int(horizon_s if horizon_s is not None else WINDOW_HORIZON_S)

    previous_horizon = detector.horizon_seconds
    detector.horizon_seconds = horizon_s
    detector._projection_cache.clear()
    try:
        scope = WindowScope(horizon_s=horizon_s)
        for train_id, tracked in detector.trains.items():
            telemetry = tracked.telemetry
            kept = 0
            for occupancy in detector.project(tracked):
                if occupancy.t_in >= horizon_s:
                    continue
                scope.intervals.append(
                    WindowInterval(
                        train_id=train_id,
                        resource_id=occupancy.resource_id,
                        seq=kept,
                        earliest_in_s=int(math.ceil(occupancy.t_in)),
                        running_s=max(
                            1,
                            int(math.ceil(occupancy.t_out))
                            - int(math.ceil(occupancy.t_in)),
                        ),
                        single_line=bool(occupancy.single_line),
                        is_loop=bool(occupancy.is_loop),
                        entry_station_id=occupancy.entry_station_id,
                        resource_length_km=float(occupancy.resource_length_km),
                        line_speed_kmh=float(occupancy.line_speed_kmh),
                        train_type=str(telemetry.get("train_type", "EXPRESS")),
                        priority_weight=float(telemetry.get("priority_weight", 6.0)),
                    )
                )
                kept += 1
    finally:
        detector.horizon_seconds = previous_horizon
        detector._projection_cache.clear()

    for interval in scope.intervals:
        scope.by_resource.setdefault(interval.resource_id, []).append(interval)
        scope.by_train.setdefault(interval.train_id, []).append(interval)

    for resource_id, group in scope.by_resource.items():
        if len({i.train_id for i in group}) >= MIN_TRAINS_FOR_CONTENTION:
            scope.contested[resource_id] = group

    for group in scope.by_resource.values():
        group.sort(key=lambda i: (i.earliest_in_s, i.train_id))
    for group in scope.by_train.values():
        group.sort(key=lambda i: i.seq)

    return scope


# ---------------------------------------------------------------------------
# Chaining
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainLink:
    """One hop along a train's route, between two resources IN THE MODEL.

    `travel_s` is the unimpeded transit between the two ENTRIES, taken from
    _prepare.earliest_arrival_s, which is the projection's t_in.

    `stop_extra_s` is decision 2's exact quantity: occupancy_from_stop_s minus
    occupancy_running_s for the run out of k -- the deceleration and
    re-acceleration the train pays for having stood at the entry to k.
    """

    train_id: str
    from_resource: str
    to_resource: str
    travel_s: int
    stop_extra_s: int


def chain_links(prepared: Dict[str, List[Any]]) -> List[ChainLink]:
    """Route order and hop constants, derived from _prepare alone."""
    by_train: Dict[str, List[Tuple[str, Any]]] = {}
    for resource_id, group in prepared.items():
        for train in group:
            by_train.setdefault(train.train_id, []).append((resource_id, train))

    links: List[ChainLink] = []
    for train_id in sorted(by_train):
        items = sorted(
            by_train[train_id], key=lambda rt: (rt[1].earliest_arrival_s, rt[0])
        )
        for (res_a, train_a), (res_b, train_b) in zip(items, items[1:]):
            links.append(
                ChainLink(
                    train_id=train_id,
                    from_resource=res_a,
                    to_resource=res_b,
                    travel_s=max(
                        0, train_b.earliest_arrival_s - train_a.earliest_arrival_s
                    ),
                    stop_extra_s=max(
                        0,
                        train_a.occupancy_from_stop_s - train_a.occupancy_running_s,
                    ),
                )
            )
    return links


# ---------------------------------------------------------------------------
# Solution
# ---------------------------------------------------------------------------


@dataclass
class GlobalSolution:
    """One solve of the whole window. Keyed by (train_id, resource_id)."""

    status: str
    feasible: bool
    entry_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    exit_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    ready_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    slack_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    wait_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    delay_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    stopped: Dict[Tuple[str, str], bool] = field(default_factory=dict)
    in_loop: Dict[Tuple[str, str], bool] = field(default_factory=dict)
    on_main: Dict[Tuple[str, str], bool] = field(default_factory=dict)
    #: Decision 1. Retained after solve, keyed (train_i, train_j, resource_id).
    precedes: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    #: The (i, j, r) triple day 10 flips for the counterfactual.
    headline: Optional[Tuple[str, str, str]] = None
    #: Decision 4. train_id -> SUM over k of slack[t,k].
    total_hold_s: Dict[str, int] = field(default_factory=dict)
    policy_exceeded: bool = False
    class_costs: Tuple[int, ...] = ()
    counts: Dict[str, int] = field(default_factory=dict)
    links: List[ChainLink] = field(default_factory=list)
    prepared: Dict[str, List[Any]] = field(default_factory=dict)
    topologies: Dict[str, Dict] = field(default_factory=dict)
    headway_s: int = DEFAULT_HEADWAY_SECONDS
    solve_count: int = 0

    def train_of(self, key: Tuple[str, str]):
        """The _ConflictTrain behind a (train_id, resource_id) key."""
        train_id, resource_id = key
        for train in self.prepared[resource_id]:
            if train.train_id == train_id:
                return train
        raise KeyError(key)

    def resources_of(self, train_id: str) -> List[str]:
        """This train's resources, in solved entry order."""
        mine = [r for (t, r) in self.entry_s if t == train_id]
        return sorted(mine, key=lambda r: (self.entry_s[(train_id, r)], r))


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _tier_cost(model, delay_var, delay_ub: int, tag: str):
    """Convex piecewise-linear delay cost as three segments. Same as enumerate.

    Slopes increase and the objective is minimised, so the solver fills the
    cheapest segment first and the segments decompose without ordering
    constraints. Standard convex piecewise-linear trick; keeps the model
    integral.
    """
    first_break, second_break = DELAY_TIER_BREAKS_S
    caps = (first_break, second_break - first_break, max(1, delay_ub))
    segments = [
        model.NewIntVar(0, size, f"seg{n}_{tag}") for n, size in enumerate(caps)
    ]
    model.Add(sum(segments) == delay_var)
    return sum(
        multiplier * segment
        for multiplier, segment in zip(DELAY_TIER_MULTIPLIERS, segments)
    )


def build_and_solve(
    payloads: Dict[str, Tuple[List[Dict], Dict]],
    pin_order: Optional[Dict[str, List[str]]] = None,
    chain: bool = True,
    objective: str = "flat",
    forbid: Optional[Tuple[str, str, str]] = None,
    hold_bound: Optional[int] = None,
    enforce_direction: bool = True,
    max_stops: Optional[int] = None,
    enforce_reachable: bool = True,
    solver_log: bool = False,
) -> GlobalSolution:
    """Build one CP-SAT model over every (train, resource) in `payloads`.

    `payloads`   resource_id -> (trains_in_conflict, topology), exactly the pair
                 detector.optimiser_inputs() returns.
    `pin_order`  resource_id -> [train_id, ...]. Fixes every precedes[i,j,r]
                 consistent with that total order. This is how the encoding gate
                 isolates the ENCODING from the objective: with the order pinned
                 the model has no freedom to choose differently, so a mismatch
                 in entry times is unambiguously an encoding error.
    `chain`      day-8 route chaining. It only bites when a train appears on
                 more than one resource in `payloads`, so a single-resource
                 solve is unaffected and stays comparable to _solve_order().
    `objective`  "flat"          the weighted tiered sum, which reproduces
                                 _solve_order under a pinned order.
                 "lexicographic" day-10 preemptive descent by IR priority class,
                                 then worst-case hold, then total delay.
    `forbid`     an (i, j, r) triple forced to 0. Day-10 counterfactual: flip
                 the headline precedence and re-solve.
    `hold_bound` hard ceiling on total_hold[t]; None uses the module policy.
    `enforce_direction`
                 decision 5's same-direction STAND_ON_MAIN ban. Dropped by the
                 relaxation path when it is the reason a plan does not exist.
    """
    prepared: Dict[str, List] = {}
    topologies: Dict[str, Dict] = {}
    for resource_id, (trains_in, topology) in payloads.items():
        prepared[resource_id] = _prepare(trains_in, topology)
        topologies[resource_id] = topology

    all_trains = [t for group in prepared.values() for t in group]
    if not all_trains:
        return GlobalSolution(status="EMPTY", feasible=False)

    headway_s = int(
        next(iter(topologies.values())).get("headway_seconds", DEFAULT_HEADWAY_SECONDS)
        if topologies else DEFAULT_HEADWAY_SECONDS
    )
    max_hold_s = int(
        next(iter(topologies.values())).get(
            "max_hold_seconds", DEFAULT_MAX_HOLD_SECONDS
        )
        if topologies else DEFAULT_MAX_HOLD_SECONDS
    )

    # Generous enough for the worst legal schedule across every resource. Too
    # small makes the model infeasible for reasons that have nothing to do with
    # the railway; too large costs nothing because delay is minimised.
    horizon = (
        max(t.earliest_arrival_s for t in all_trains)
        + sum(t.occupancy_from_stop_s + headway_s for t in all_trains)
        + max_hold_s
        + 1
    )

    links = chain_links(prepared) if chain else []
    predecessor: Dict[Tuple[str, str], ChainLink] = {
        (link.train_id, link.to_resource): link for link in links
    }

    # Forced delay is only a constant once the order is fixed. Unpinned there is
    # no per-solve baseline, so the per-interval ceiling is the horizon and the
    # anti-starvation statement moves to total_hold[t] below.
    cap: Dict[Tuple[str, str], int] = {}
    for resource_id, group in prepared.items():
        by_id = {t.train_id: t for t in group}
        order = (pin_order or {}).get(resource_id)
        if order:
            block_free_at = 0
            for train_id in order:
                train = by_id[train_id]
                start = max(block_free_at, train.earliest_arrival_s)
                forced = start - train.earliest_arrival_s
                cap[(train_id, resource_id)] = forced + max_hold_s
                block_free_at = start + train.occupancy_running_s + headway_s
        else:
            for train in group:
                cap[(train.train_id, resource_id)] = horizon

    model = cp_model.CpModel()
    entry, exit_, occupancy, stopped = {}, {}, {}, {}
    in_loop, on_main, ready, slack, wait, delay, interval = {}, {}, {}, {}, {}, {}, {}
    spec: Dict[Tuple[str, str], Any] = {}

    # ---- item 5: one interval per (train, resource) -----------------------
    for resource_id, group in prepared.items():
        for train in group:
            key = (train.train_id, resource_id)
            spec[key] = train
            tag = f"{train.train_id}_{resource_id}"

            entry[key] = model.NewIntVar(
                train.earliest_arrival_s, horizon, f"entry_{tag}"
            )
            # Decision 2: brought to a stand at the ENTRY to this resource.
            stopped[key] = model.NewBoolVar(f"stopped_{tag}")
            in_loop[key] = model.NewBoolVar(f"in_loop_{tag}")
            on_main[key] = model.NewBoolVar(f"on_main_{tag}")
            model.Add(in_loop[key] + on_main[key] == stopped[key])
            if not train.loop_available:
                model.Add(in_loop[key] == 0)

            occupancy[key] = model.NewIntVar(
                train.occupancy_running_s,
                max(train.occupancy_from_stop_s, train.occupancy_running_s),
                f"occ_{tag}",
            )
            model.Add(
                occupancy[key]
                == train.occupancy_running_s
                + (train.occupancy_from_stop_s - train.occupancy_running_s)
                * stopped[key]
            )

            exit_[key] = model.NewIntVar(0, horizon, f"exit_{tag}")
            model.Add(exit_[key] == entry[key] + occupancy[key])
            interval[key] = model.NewIntervalVar(
                entry[key], occupancy[key], exit_[key], f"iv_{tag}"
            )

            # ---- ready, then slack against it -----------------------------
            # ready[t,k] is when the chained schedule delivers this train to the
            # entry of k. For the first resource in the model that is the
            # unimpeded projection; after a hold upstream it is that hold's
            # consequence, and the hold has already been charged where it was
            # imposed.
            ready[key] = model.NewIntVar(
                train.earliest_arrival_s, horizon, f"ready_{tag}"
            )
            slack[key] = model.NewIntVar(0, horizon, f"slack_{tag}")
            model.Add(slack[key] == entry[key] - ready[key])

            # wait stays the CUMULATIVE lateness at k. It is what the
            # controller-facing numbers and the encoding gate compare, and it is
            # deliberately NOT what total_hold sums: summing it would charge one
            # upstream hold again at every resource downstream of it.
            wait[key] = model.NewIntVar(0, horizon, f"wait_{tag}")
            model.Add(wait[key] == entry[key] - train.earliest_arrival_s)

            absorbable = train.absorbable_s
            incoming = predecessor.get(key)
            if CHAIN_LOCAL_ABSORB and incoming is not None:
                absorbable = min(
                    absorbable,
                    int(
                        incoming.travel_s
                        * (1.0 / kin.MIN_REGULATION_FRACTION - 1.0)
                    ),
                )
            # A train may only avoid stopping if the slack IMPOSED HERE is small
            # enough to be absorbed by running slower on the approach. Against
            # slack, not wait: a train already late because it was held upstream
            # does not have to stop again for that same delay.
            model.Add(slack[key] <= absorbable).OnlyEnforceIf(stopped[key].Not())
            model.Add(slack[key] >= absorbable + 1).OnlyEnforceIf(stopped[key])

            delay[key] = model.NewIntVar(0, horizon, f"delay_{tag}")
            model.Add(
                delay[key]
                == slack[key]
                + train.loop_stop_penalty_s * in_loop[key]
                + train.main_stop_penalty_s * on_main[key]
            )
            if pin_order and resource_id in pin_order:
                model.Add(delay[key] <= cap[key])

    # ---- item 8.3 and 9.1: chaining, with the stop penalty on the hop -----
    # entry[k+1] >= entry[k] + travel + (from_stop - running) * stopped[k],
    # written as an equality on ready[k+1] so slack[k+1] is exactly the time the
    # model chose to leave the train standing at the entry to k+1 and nothing
    # else. Equality is safe: slack[k+1] >= 0 supplies the inequality.
    for link in links:
        here = (link.train_id, link.from_resource)
        there = (link.train_id, link.to_resource)
        if here in entry and there in ready:
            model.Add(
                ready[there]
                == entry[here] + link.travel_s + link.stop_extra_s * stopped[here]
            )
    for key, train in spec.items():
        if key not in predecessor:
            model.Add(ready[key] == train.earliest_arrival_s)

    # ---- item 7: NoOverlap per resource -----------------------------------
    for resource_id, group in prepared.items():
        ivs = [interval[(t.train_id, resource_id)] for t in group]
        if len(ivs) > 1:
            model.AddNoOverlap(ivs)

    # ---- item 6: precedes[i,j,r], ordered, retained ------------------------
    precedes: Dict[Tuple[str, str, str], Any] = {}
    for resource_id, group in prepared.items():
        by_id = {t.train_id: t for t in group}
        ids = [t.train_id for t in group]
        for first, second in combinations(ids, 2):
            forward = model.NewBoolVar(f"prec_{first}_{second}_{resource_id}")
            backward = model.NewBoolVar(f"prec_{second}_{first}_{resource_id}")
            precedes[(first, second, resource_id)] = forward
            precedes[(second, first, resource_id)] = backward
            model.Add(forward + backward == 1)
            model.Add(
                entry[(second, resource_id)]
                >= exit_[(first, resource_id)] + headway_s
            ).OnlyEnforceIf(forward)
            model.Add(
                entry[(first, resource_id)]
                >= exit_[(second, resource_id)] + headway_s
            ).OnlyEnforceIf(backward)

            # ---- decision 5: no same-direction STAND_ON_MAIN --------------
            # The injector refuses it, and it is right to: standing on the
            # running line to be overtaken puts the held train in front of the
            # train doing the overtaking. An overtake needs a loop.
            #
            # THREE guards, each of which was a measured failure without it:
            #
            #   not pin_order        the pin exists to reproduce _solve_order,
            #                        which has no such rule. Applying it under a
            #                        pin fails the encoding gate on a constraint
            #                        enumerate was never asked to satisfy.
            #   loop_available       forbidding the main-line stand for a train
            #                        with nowhere else to stand removes its only
            #                        option. Measured: BLK-115D went INFEASIBLE,
            #                        two DOWN trains, no loop at the entry
            #                        station. "No advice" is worse than a
            #                        directive the injector refuses.
            #   enforce_direction    caller-level escape, used by the relaxation
            #                        in solve_with_policy when even a loop-owning
            #                        fleet cannot satisfy it (two same-direction
            #                        trains contending one loop).
            #
            # Direction missing means unknown, and an unknown must not forbid a
            # legal plan.
            dir_a = getattr(by_id[first], "direction", None)
            dir_b = getattr(by_id[second], "direction", None)
            if enforce_direction and not pin_order and dir_a and dir_b and dir_a == dir_b:
                if by_id[second].loop_available:
                    model.Add(
                        on_main[(second, resource_id)] == 0
                    ).OnlyEnforceIf(forward)
                if by_id[first].loop_available:
                    model.Add(
                        on_main[(first, resource_id)] == 0
                    ).OnlyEnforceIf(backward)

    for resource_id, order in (pin_order or {}).items():
        position = {train_id: n for n, train_id in enumerate(order)}
        for (first, second, res), var in precedes.items():
            if res != resource_id:
                continue
            if first in position and second in position:
                model.Add(var == (1 if position[first] < position[second] else 0))

    if forbid is not None and forbid in precedes:
        model.Add(precedes[forbid] == 0)

    # ---- decision 5: loop capacity across the whole window -----------------
    # Per loop_id, not per conflict, and over the STANDING window
    # [ready, entry] rather than [earliest_arrival, entry]. Under chaining those
    # differ: a train held at k reaches k+1 late, and charging the berth from
    # its unimpeded arrival would book the loop for time the train spent
    # running. This is also what stops one train booking one loop on two
    # adjacent blocks of its own route -- it stands once, so exactly one
    # in_loop is set and the other interval has zero slack.
    loop_intervals: Dict[str, List] = {}
    for resource_id, group in prepared.items():
        for train in group:
            if not train.loop_available or not train.loop_id:
                continue
            key = (train.train_id, resource_id)
            loop_intervals.setdefault(train.loop_id, []).append(
                model.NewOptionalIntervalVar(
                    ready[key], slack[key], entry[key], in_loop[key],
                    f"loop_{train.train_id}_{resource_id}",
                )
            )
    for ivs in loop_intervals.values():
        if len(ivs) > 1:
            model.AddNoOverlap(ivs)

    # ---- decision 4: total_hold[t] ----------------------------------------
    by_train_keys: Dict[str, List[Tuple[str, str]]] = {}
    for key in entry:
        by_train_keys.setdefault(key[0], []).append(key)

    if hold_bound is None:
        hold_bound = (
            int(max_hold_s * GLOBAL_HOLD_CAP_MULTIPLIER)
            if GLOBAL_HOLD_CAP_MULTIPLIER > 0
            else 0
        )

    total_hold: Dict[str, Any] = {}
    for train_id, keys in by_train_keys.items():
        total_hold[train_id] = model.NewIntVar(0, horizon, f"hold_{train_id}")
        model.Add(total_hold[train_id] == sum(slack[k] for k in keys))
        # Never under a pinned order: the pin exists to reproduce enumerate,
        # and enumerate has no cumulative constraint to reproduce.
        if hold_bound and not pin_order:
            model.Add(total_hold[train_id] <= hold_bound)

    worst_hold = model.NewIntVar(0, horizon, "worst_hold")
    model.AddMaxEquality(worst_hold, list(total_hold.values()))
    # One stand per train per window. Not applied under a pin: the pin exists
    # to reproduce _solve_order, which has one interval per train and so no
    # opinion on this.
    stop_ceiling = GLOBAL_MAX_STOPS if max_stops is None else max_stops
    if stop_ceiling > 0 and not pin_order:
        for train_id, keys in by_train_keys.items():
            if len(keys) > 1:
                model.Add(sum(stopped[k] for k in keys) <= stop_ceiling)

    # ---- objective --------------------------------------------------------
    # The convex tier applies to a TRAIN, not to an interval. This was wrong
    # for one release and the cost was concrete: with the tiers priced per
    # interval, splitting a train's delay across two resources refills the
    # cheap x1 and x2 bands twice, and the model buys that discount with an
    # extra stop. Measured on scenario.json: 12626 stood on the main at
    # BLK-115D *and* berthed at PWL for SEC-PWL-KSV, total 2,614 s against
    # enumerate's 2,407 s for one stop -- a later train at a lower objective.
    #
    #   one stop   tier(2407)              = 8128
    #   two stops  tier(859) + tier(1755)  = 7456
    #
    # Convexity is a statement about a train's lateness. Per interval it is a
    # statement about nothing. Summing delay first restores it, and costs
    # nothing in the isolated gate: one interval per train there, so the two
    # formulations are the same expression.
    train_of_id: Dict[str, Any] = {}
    for group in prepared.values():
        for train in group:
            train_of_id.setdefault(train.train_id, train)

    train_delay: Dict[str, Any] = {}
    for train_id, keys in by_train_keys.items():
        ceiling = horizon * len(keys)
        var = model.NewIntVar(0, ceiling, f"traindelay_{train_id}")
        model.Add(var == sum(delay[k] for k in keys))
        train_delay[train_id] = var

    class_terms: Dict[int, List[Any]] = {}
    flat_terms: List[Any] = []
    for train_id, var in train_delay.items():
        train = train_of_id[train_id]
        cost = _tier_cost(
            model, var, horizon * len(by_train_keys[train_id]), train_id
        )
        weighted = train.weight_scaled * cost
        flat_terms.append(weighted)
        flat_terms.append(TIEBREAK_COEFFICIENT * var)
        class_terms.setdefault(priority_class(train.train_type), []).append(weighted)

    # ONE budget for the whole descent, not one per solve. The lexicographic
    # path runs a solve per present class plus two tiers, and optimize_global
    # runs the whole thing again for the counterfactual -- so a 10 s per-solve
    # limit is a 220 s worst case against a 2 s merge gate.
    #
    # The pinned/flat path keeps a generous limit: the encoding gate needs
    # OPTIMAL, and a truncated solve returns FEASIBLE and fails it for a
    # reason that is not an encoding error.
    expected_solves = (len(class_terms) + 2) if objective == "lexicographic" else 1
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = SOLVER_WORKERS
    solver.parameters.log_search_progress = solver_log

    if objective == "lexicographic":
        tier_budget = GLOBAL_TIER_BUDGET_S
        if GLOBAL_DET_BUDGET > 0:
            solver.parameters.max_deterministic_time = GLOBAL_DET_BUDGET
    else:
        tier_budget = max(SOLVER_TIME_LIMIT_S, 10.0)
        if SOLVER_DETERMINISTIC_TIME > 0:
            solver.parameters.max_deterministic_time = SOLVER_DETERMINISTIC_TIME
    solver.parameters.max_time_in_seconds = tier_budget

    names = {
        cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    counts = {
        "resources": len(prepared),
        "intervals": len(interval),
        "precedes": len(precedes),
        "loops": len(loop_intervals),
        "links": len(links),
    }

    def _capture(code) -> GlobalSolution:
        """Freeze the solver's current values. Called after every tier that
        succeeds, so a later tier timing out costs us that tier's refinement
        and nothing else -- never the class-optimal schedule already in hand.
        """
        captured = GlobalSolution(
            status=names.get(code, str(code)), feasible=True, counts=dict(counts),
            links=links, prepared=prepared, topologies=topologies,
            headway_s=headway_s,
        )
        for k in interval:
            captured.entry_s[k] = solver.Value(entry[k])
            captured.exit_s[k] = solver.Value(exit_[k])
            captured.ready_s[k] = solver.Value(ready[k])
            captured.slack_s[k] = solver.Value(slack[k])
            captured.wait_s[k] = solver.Value(wait[k])
            captured.delay_s[k] = solver.Value(delay[k])
            captured.stopped[k] = bool(solver.Value(stopped[k]))
            captured.in_loop[k] = bool(solver.Value(in_loop[k]))
            captured.on_main[k] = bool(solver.Value(on_main[k]))
        for triple, var in precedes.items():
            captured.precedes[triple] = solver.Value(var)
        for tid, var in total_hold.items():
            captured.total_hold_s[tid] = solver.Value(var)
        return captured

    def _hint_from(known: GlobalSolution) -> None:
        """Warm-start the next tier with the tier above it.

        CP-SAT restarts search from nothing on every Solve() call -- it carries
        no state between invocations. Without this, each tier re-derives a
        feasible schedule for the whole window from scratch, PLUS the frozen
        class_cost equality, inside its slice of the budget. Measured: the
        descent completed 1 of 6 tiers at every budget from 0.6 s to 2.0 s, so
        worst_hold (tier 4) never ran and 12280 carried 22,707 s of standing.

        The previous tier's solution is feasible for this one by construction --
        freezing class_cost at its optimum cannot exclude the solution that
        achieved it -- so the hint is always valid, never just plausible.
        """
        model.ClearHints()
        for k in interval:
            model.AddHint(entry[k], known.entry_s[k])
            model.AddHint(stopped[k], int(known.stopped[k]))
            model.AddHint(in_loop[k], int(known.in_loop[k]))
            model.AddHint(on_main[k], int(known.on_main[k]))
        for triple, var in precedes.items():
            model.AddHint(var, known.precedes[triple])

    best: Optional[GlobalSolution] = None
    solve_count = 0
    tiers_completed = 0
    truncated = False

    if objective == "lexicographic":
        cost_ceiling = max(
            1, sum(t.weight_scaled for t in all_trains) * max(DELAY_TIER_MULTIPLIERS)
        ) * horizon
        class_vars: Dict[int, Any] = {}
        for cls, terms in class_terms.items():
            var = model.NewIntVar(0, cost_ceiling, f"classcost_{cls}")
            model.Add(var == sum(terms))
            class_vars[cls] = var

        tiers: List[Tuple[str, Any]] = [
            (f"class{cls}", class_vars[cls]) for cls in sorted(class_vars, reverse=True)
        ]
        tiers.append(("worst_hold", worst_hold))
        tiers.append(("total_delay", sum(delay.values())))

        # Per-tier record. Without it a starved descent is invisible: the
        # returned status is tier 0's, so a plan that skipped worst_hold
        # entirely reports OPTIMAL. That is how 12280 came to carry 22,707 s.
        tier_log: List[str] = []
        for n, (tier_name, expr) in enumerate(tiers):
            model.Minimize(expr)
            t_tier = time.perf_counter()
            status = solver.Solve(model)
            elapsed_ms = (time.perf_counter() - t_tier) * 1000.0
            solve_count += 1
            tier_log.append(
                f"{tier_name}:{names.get(status, status)}@{elapsed_ms:.0f}ms"
            )
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                # A tier that ran out of time costs us that tier's refinement.
                # It must not cost us the tiers above it, which are the ones
                # the IR precedence rule actually cares about.
                truncated = True
                break
            best = _capture(status)
            tiers_completed += 1
            if status == cp_model.FEASIBLE:
                truncated = True
            if n < len(tiers) - 1:
                model.Add(expr == solver.Value(expr))
                _hint_from(best)
    else:
        status = solver.Solve(model)
        solve_count += 1
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            best = _capture(status)
            tiers_completed = 1
            truncated = status == cp_model.FEASIBLE

    counts["solves"] = solve_count
    counts["tiers_completed"] = tiers_completed
    counts["tiers_total"] = expected_solves
    counts["truncated"] = int(truncated)
    counts["tier_log"] = "; ".join(tier_log) if objective == "lexicographic" else ""

    if best is None:
        return GlobalSolution(
            status=names.get(status, str(status)), feasible=False, counts=counts,
            links=links, prepared=prepared, topologies=topologies,
            headway_s=headway_s, solve_count=solve_count,
        )

    solution = best
    solution.counts = counts
    solution.solve_count = solve_count

    costs = [0] * PRIORITY_CLASS_COUNT
    for resource_id, group in prepared.items():
        for train in group:
            costs[priority_class(train.train_type)] += (
                train.weight_scaled * solution.delay_s[(train.train_id, resource_id)]
            )
    solution.class_costs = tuple(reversed(costs))
    solution.headline = _headline(solution, prepared)
    return solution


def _headline(
    solution: GlobalSolution, prepared: Dict[str, List[Any]]
) -> Optional[Tuple[str, str, str]]:
    """The (i, j, r) day 10 flips: the leading pair on the busiest resource."""
    if not prepared:
        return None
    busiest = max(prepared, key=lambda r: (len(prepared[r]), r))
    ordered = sorted(
        (t.train_id for t in prepared[busiest]),
        key=lambda tid: (solution.entry_s[(tid, busiest)], tid),
    )
    if len(ordered) < 2:
        return None
    return (ordered[0], ordered[1], busiest)

def _flag_starvation(solution: GlobalSolution) -> GlobalSolution:
    """Report starvation rather than refusing to plan around it.

    A hard ceiling forces a second full descent on every tick -- ~7 s becomes
    ~14 s, doubled again by the counterfactual -- and risks returning nothing
    at all. A controller is better served by a plan that says what is wrong
    with it than by a blank screen.

    The hard bound stays available via hold_bound, because
    tests/test_global_hold.py needs a model that refuses: that test's whole
    argument is that a composed plan the per-conflict engine approves is one
    the global model will not accept.
    """
    if not solution.feasible or GLOBAL_STARVATION_THRESHOLD_S <= 0:
        return solution
    
    starved = sorted(
        train_id for train_id, held in solution.total_hold_s.items()
        if held > GLOBAL_STARVATION_THRESHOLD_S
    )
    solution.counts["worst_hold_s"] = max(solution.total_hold_s.values(), default=0)
    
    if starved:
        solution.policy_exceeded = True
        solution.counts["starved"] = ",".join(starved)
        
    return solution


def solve_with_policy(
    payloads: Dict[str, Tuple[List[Dict], Dict]],
    objective: str = "lexicographic",
    forbid: Optional[Tuple[str, str, str]] = None,
) -> GlobalSolution:
    """Solve, relaxing rather than returning nothing. Two ladders, in order.

    Mirrors optimize_precedence: a plan that breaks a guideline beats no plan,
    provided it says so.

      1. the cumulative-hold ceiling, lifted by HOLD_RELAX_MULTIPLIER
      2. decision 5's same-direction ban, dropped entirely

    The direction ban goes last because it is the one whose violation the
    SIMULATOR will catch: a refused STAND_ON_MAIN costs one directive, whereas
    an infeasible model costs the controller every card on the screen. Both
    outcomes are labelled policy_exceeded so the card says which.
    """
    solution = build_and_solve(payloads, objective=objective, forbid=forbid)
    if solution.feasible:
        return _flag_starvation(solution)

    if GLOBAL_HOLD_CAP_MULTIPLIER > 0:
        max_hold_s = int(
            next(iter(payloads.values()))[1].get(
                "max_hold_seconds", DEFAULT_MAX_HOLD_SECONDS
            )
        )
        relaxed = int(
            max_hold_s * GLOBAL_HOLD_CAP_MULTIPLIER * HOLD_RELAX_MULTIPLIER
        )
        solution = build_and_solve(
            payloads, objective=objective, forbid=forbid, hold_bound=relaxed
        )
        if solution.feasible:
            solution.policy_exceeded = True
            return _flag_starvation(solution)

    relaxed_stops = build_and_solve(
        payloads, objective=objective, forbid=forbid,
        hold_bound=0, max_stops=0,
    )
    if relaxed_stops.feasible:
        relaxed_stops.policy_exceeded = True
        return _flag_starvation(relaxed_stops)
    
    relaxed_reach = build_and_solve(
        payloads, objective=objective, forbid=forbid,
        hold_bound=0, enforce_reachable=False,
    )
    if relaxed_reach.feasible:
        relaxed_reach.policy_exceeded = True
        return _flag_starvation(relaxed_reach)

    solution = build_and_solve(
        payloads, objective=objective, forbid=forbid,
        hold_bound=0, enforce_direction=False,
    )
    solution.policy_exceeded = solution.feasible
    return _flag_starvation(solution)


# ---------------------------------------------------------------------------
# Day 11 -- directive emission and card decomposition
# ---------------------------------------------------------------------------


def _binding_predecessor(
    solution: GlobalSolution,
    train_id: str,
    resource_id: str,
    opposite_only: bool = False,
) -> Optional[str]:
    """The train whose exit this train's entry is waiting on, at this resource.

    Named on the directive as until_train_id, so the simulator's release rule
    and the controller's card agree on WHO is being waited for.

    `opposite_only` is the STAND_ON_MAIN case. The injector refuses a main-line
    stand whose until_train_id runs in the SAME direction, and refuses nothing
    when until_train_id is absent. So when the binding predecessor is
    same-direction, name an opposite-direction one if the resource has one and
    otherwise omit the field: the release_timeout_seconds backstop still
    discharges the stand. Emitting a directive we know will be dropped is worse
    than emitting one that releases on a timer.
    """
    mine = solution.train_of((train_id, resource_id))
    my_direction = getattr(mine, "direction", None)

    def usable(candidate_id: str) -> bool:
        if not opposite_only or not my_direction:
            return True
        other = getattr(
            solution.train_of((candidate_id, resource_id)), "direction", None
        )
        return not other or other != my_direction

    my_entry = solution.entry_s[(train_id, resource_id)]
    tight = sorted(
        a for (a, b, res), value in solution.precedes.items()
        if res == resource_id and b == train_id and value
        and my_entry == solution.exit_s[(a, resource_id)] + solution.headway_s
    )
    for candidate in tight:
        if usable(candidate):
            return candidate

    ids = [
        t.train_id for t in solution.prepared.get(resource_id, [])
        if t.train_id != train_id and usable(t.train_id)
    ]
    if not ids:
        return None
    return min(ids, key=lambda tid: (solution.entry_s[(tid, resource_id)], tid))


def _motivating_resource(solution: GlobalSolution, train_id: str) -> Optional[str]:
    """Decision 3: the resource whose precedence decision caused this hold.

    A directive is attributed to the resource where THIS train's ordering
    constraint is tight -- entry equals some predecessor's exit plus headway.
    Where several bind, the largest contention wins; ties break on resource_id
    so attribution is deterministic. Where none binds, the resource carrying the
    most slack.
    """
    binding: List[Tuple[int, str]] = []
    loose: List[Tuple[int, str]] = []
    for (tid, resource_id), slack_s in solution.slack_s.items():
        if tid != train_id or slack_s <= 0:
            continue
        loose.append((-slack_s, resource_id))
        my_entry = solution.entry_s[(tid, resource_id)]
        for (a, b, res), value in solution.precedes.items():
            if res != resource_id or b != train_id or not value:
                continue
            if my_entry == solution.exit_s[(a, resource_id)] + solution.headway_s:
                binding.append((-len(solution.prepared.get(resource_id, [])), resource_id))
                break
    if binding:
        return sorted(binding)[0][1]
    return sorted(loose)[0][1] if loose else None


def emit_directives(solution: GlobalSolution) -> List[Dict[str, Any]]:
    """EXACTLY ONE directive per train, each carrying motivating_resource_id.

    Enumerate emitted one per conflict and the same train collected three
    disagreeing regulation targets in a single evaluate (measured day 2:
    contradictory_instructions = 3). Here the plan is one schedule and the
    simulator holds one state per train, so a train gets one instruction: the
    intervention that comes FIRST along its route.

    A train the plan holds at resource k AND regulates approaching resource m
    receives only the hold. The regulation is a consequence of a decision the
    train has not reached yet; it belongs to a later evaluate. Emitting both at
    once is not a plan the railway can execute -- it is two instructions
    racing, and the simulator resolves the race by discarding the hold.
    """
    if not solution.feasible:
        return []

    by_train: Dict[str, List[Tuple[str, str]]] = {}
    for key in solution.slack_s:
        by_train.setdefault(key[0], []).append(key)

    directives: List[Dict[str, Any]] = []
    for train_id in sorted(by_train):
        keys = sorted(by_train[train_id], key=lambda k: (solution.entry_s[k], k[1]))

        # ONE directive per train, and it must be the intervention that comes
        # FIRST along the route. The simulator holds one state per train:
        # _drain_directives sets hold_station_id = None on any REGULATE, so a
        # stand and a regulation submitted together cancel each other and the
        # later one wins. Measured on scenario10 tick 0: 4 of 5 stands silently
        # erased by the train's own regulation, with no refusal message from
        # the simulator at all.
        #
        # This is not a wiring problem. A stand at resource k and a regulation
        # approaching resource m are SEQUENTIAL, and submit_directive applies
        # everything at once. The downstream instruction belongs to a later
        # evaluate, after the release, when the train's position has advanced
        # -- which is also how a controller issues them.
        chosen: Optional[Tuple[str, Tuple[str, str]]] = None
        for k in keys:
            if solution.slack_s[k] <= 0:
                continue
            if solution.stopped[k]:
                # Belt and braces on top of the reachability constraint: a
                # stand the simulator will drop or silently re-target is worse
                # than no instruction. The slack stays in total_hold either
                # way; what is lost is the directive, and the card must not
                # claim an action the railway cannot take.
                if solution.train_of(k).distance_m <= HOLD_MIN_APPROACH_M:
                    solution.counts["unreachable_stands"] = (
                        solution.counts.get("unreachable_stands", 0) + 1
                    )
                    continue
                chosen = ("STAND", k)
            else:
                chosen = ("REGULATE", k)
            break
        if chosen is None:
            continue

        mode, key = chosen
        _, resource_id = key
        train = solution.train_of(key)
        motivating = _motivating_resource(solution, train_id) or resource_id

        if mode == "REGULATE":
            # Only the slack at THIS resource. Aggregating slack the train will
            # not reach until after a downstream decision prices a regulation
            # against time it has not yet had.
            directives.append({
                "kind": "REGULATE", "train_id": train_id,
                "target_speed_kmh": float(round(
                    kin.regulated_speed_kmh(
                        train.distance_m, train.speed_ms, solution.slack_s[key]
                    )
                )),
                "motivating_resource_id": motivating,
            })
            continue

        timeout = solution.delay_s[key] + DIRECTIVE_RELEASE_TIMEOUT_S
        until = _binding_predecessor(
            solution, train_id, resource_id,
            opposite_only=bool(solution.on_main[key]),
        )
        if solution.in_loop[key]:
            directives.append({
                "kind": "HOLD_AT_LOOP", "train_id": train_id,
                "station_id": train.loop_station, "loop_id": train.loop_id,
                "until_train_id": until,
                "release_timeout_seconds": timeout,
                "motivating_resource_id": motivating,
            })
        else:
            station = train.approach_station or solution.topologies.get(
                resource_id, {}
            ).get("junction_id")
            if station:
                directives.append({
                    "kind": "STAND_ON_MAIN", "train_id": train_id,
                    "station_id": station, "until_train_id": until,
                    "release_timeout_seconds": timeout,
                    "motivating_resource_id": motivating,
                })

    return directives


def _scenario_from(
    solution: GlobalSolution,
    resource_id: str,
    train_ids: Sequence[str],
    directives: List[Dict[str, Any]],
    scenario_id: str,
    rank: int,
    rationale: str,
) -> Dict[str, Any]:
    """One controller card: the directives this conflict motivated, plus impact."""
    members = set(train_ids)
    mine = [
        d for d in directives
        if d.get("motivating_resource_id") == resource_id and d["train_id"] in members
    ]

    impacts: List[str] = []
    breakdown: List[Dict[str, Any]] = []
    clauses: List[Tuple[int, str]] = []
    for train_id in sorted(members):
        key = (train_id, resource_id)
        if key not in solution.delay_s:
            continue
        train = solution.train_of(key)
        delay_s = solution.delay_s[key]
        slack_s = solution.slack_s[key]
        minutes = round(delay_s / 60)
        breakdown.append({
            "train_id": train_id,
            "train_name": train.train_name,
            "delay_seconds": delay_s,
            # Under chaining the split is no longer forced-vs-chosen: it is
            # lateness carried in from upstream versus the stand imposed HERE.
            "queued_seconds": max(0, solution.wait_s[key] - slack_s),
            "dispatch_choice_seconds": slack_s,
            "cumulative_hold_seconds": solution.total_hold_s.get(train_id, 0),
        })
        impacts.append(f"{train.train_name} {train_id} delayed by {minutes} min")
        if solution.in_loop[key]:
            at = f" at {train.loop_station}" if train.loop_station else ""
            clauses.append((
                delay_s,
                f"Hold {train.train_name} {train_id} at {train.loop_id}{at} "
                f"for {minutes} min",
            ))
        elif solution.on_main[key]:
            clauses.append((
                delay_s,
                f"Stand {train.train_name} {train_id} on the running line short "
                f"of {resource_id} for {minutes} min",
            ))
        elif slack_s > 0:
            target = round(
                kin.regulated_speed_kmh(train.distance_m, train.speed_ms, slack_s)
            )
            clauses.append((
                delay_s,
                f"Regulate {train.train_name} {train_id} to {target} km/h "
                f"on approach ({minutes} min)",
            ))

    lead = min(
        (t for t in members if (t, resource_id) in solution.entry_s),
        key=lambda t: (solution.entry_s[(t, resource_id)], t),
        default=None,
    )
    if clauses:
        clauses.sort(reverse=True)
        action = "; ".join(
            [clauses[0][1]] + [_lower_first(text) for _, text in clauses[1:]]
        )
    else:
        action = f"Clear {lead} through {resource_id} without regulation"

    return {
        "scenario_id": scenario_id,
        "rank": rank,
        "action": action,
        "rationale": rationale,
        "network_impact": ". ".join(impacts) + ("." if impacts else ""),
        "policy_exceeded": solution.policy_exceeded,
        "directives": mine,
        "delay_breakdown": breakdown,
        "order_train_ids": sorted(
            (t for t in members if (t, resource_id) in solution.entry_s),
            key=lambda t: (solution.entry_s[(t, resource_id)], t),
        ),
    }


def _class_rationale(solution: GlobalSolution, resource_id: str) -> str:
    trains = solution.prepared.get(resource_id, [])
    if not trains:
        return "Global plan"
    by_class: Dict[int, List[Any]] = {}
    for train in trains:
        by_class.setdefault(priority_class(train.train_type), []).append(train)
    top = max(by_class)
    named = ", ".join(f"{t.train_name} {t.train_id}" for t in by_class[top])
    label = CLASS_LABELS.get(top, f"class {top}")
    clear = all(
        solution.delay_s[(t.train_id, resource_id)] < 60 for t in by_class[top]
    )
    verb = "runs unimpeded" if clear else "takes precedence"
    return (
        f"{label} ({named}) {verb}; solved jointly across "
        f"{len(solution.prepared)} contested resource(s) in one precedence model"
    )


def optimize_global(
    detector,
    candidates: Dict[str, Dict[str, Any]],
    max_scenarios: int = 2,
) -> Dict[str, List[Dict[str, Any]]]:
    """One solve for every raised conflict, decomposed back into cards.

    Returns conflict_id -> [scenario, ...] in the shape optimize_precedence
    returns, so evaluate() publishes the same contract either way and the
    controller still chooses OPT-1 or OPT-2 per card. Decision 3 is what makes
    the decomposition possible: every directive carries the resource whose
    precedence decision caused it, and the card is a groupby on that field.
    """
    payloads: Dict[str, Tuple[List[Dict], Dict]] = {}
    resource_of: Dict[str, str] = {}
    trains_on: Dict[str, List[str]] = {}
    for conflict_id, conflict in candidates.items():
        resource_id = conflict["resource_id"]
        trains_in, topology = detector.optimiser_inputs(conflict)
        if len(trains_in) < 2:
            continue
        resource_of[conflict_id] = resource_id
        if resource_id in payloads:
            continue
        payloads[resource_id] = (trains_in, topology)
        trains_on[resource_id] = [t["train_id"] for t in trains_in]

    if not payloads:
        return {}

    solution = solve_with_policy(payloads, objective="lexicographic")
    if not solution.feasible:
        return {}
    directives = emit_directives(solution)

    counter: Optional[GlobalSolution] = None
    counter_directives: List[Dict[str, Any]] = []
    if solution.headline is not None and max_scenarios > 1:
        counter = solve_with_policy(
            payloads, objective="lexicographic", forbid=solution.headline
        )
        if counter.feasible:
            counter_directives = emit_directives(counter)

    out: Dict[str, List[Dict[str, Any]]] = {}
    for conflict_id, resource_id in resource_of.items():
        ids = trains_on.get(resource_id, [])
        if not ids:
            continue
        best = _scenario_from(
            solution, resource_id, ids, directives, "OPT-1", 1,
            _class_rationale(solution, resource_id),
        )
        scenarios = [best]
        if (
            counter is not None and counter.feasible
            and solution.headline is not None
            and solution.headline[2] == resource_id
        ):
            first, second, _res = solution.headline
            alternative = _scenario_from(
                counter, resource_id, ids, counter_directives, "OPT-2", 2,
                f"Counterfactual: {second} takes {resource_id} ahead of {first}",
            )
            if alternative["delay_breakdown"] != best["delay_breakdown"]:
                scenarios.append(alternative)
        out[conflict_id] = scenarios
    return out