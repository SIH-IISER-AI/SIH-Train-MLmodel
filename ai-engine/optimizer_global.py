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

import math
import os
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from optimizer import (
    DEFAULT_HEADWAY_SECONDS,
    DEFAULT_MAX_HOLD_SECONDS,
    SOLVER_DETERMINISTIC_TIME,
    SOLVER_TIME_LIMIT_S,
    SOLVER_WORKERS,
    DELAY_TIER_BREAKS_S,
    DELAY_TIER_MULTIPLIERS,
    TIEBREAK_COEFFICIENT,
    _prepare,
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


@dataclass
class GlobalSolution:
    """One solve of the whole window. Keyed by (train_id, resource_id)."""

    status: str
    feasible: bool
    entry_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    exit_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    wait_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    delay_s: Dict[Tuple[str, str], int] = field(default_factory=dict)
    stopped: Dict[Tuple[str, str], bool] = field(default_factory=dict)
    in_loop: Dict[Tuple[str, str], bool] = field(default_factory=dict)
    on_main: Dict[Tuple[str, str], bool] = field(default_factory=dict)
    #: Decision 1. Retained after solve, keyed (train_i, train_j, resource_id).
    precedes: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    #: The (i, j, r) triple day 10 flips for the counterfactual.
    headline: Optional[Tuple[str, str, str]] = None
    counts: Dict[str, int] = field(default_factory=dict)


def build_and_solve(
    payloads: Dict[str, Tuple[List[Dict], Dict]],
    pin_order: Optional[Dict[str, List[str]]] = None,
    solver_log: bool = False,
) -> GlobalSolution:
    """Days 6-7: interval vars, precedence booleans, NoOverlap. No chaining.

    `payloads`   resource_id -> (trains_in_conflict, topology), exactly the
                 pair detector.optimiser_inputs() returns. Constants come from
                 optimizer._prepare() unchanged, so a single-resource solve is
                 directly comparable to _solve_order().
    `pin_order`  resource_id -> [train_id, ...]. Fixes every precedes[i,j,r]
                 consistent with that total order. This is how the day-7 gate
                 isolates the ENCODING from the objective: with the order
                 pinned the model has no freedom to choose differently, so a
                 mismatch in entry times is unambiguously an encoding error
                 rather than the global model coordinating better.

    There is deliberately NO dispatch objective here. The lexicographic descent
    is day 10. Minimising the sum of entry times is not a stand-in for it --
    it carries no priority weights and no delay tiers. It exists only to make
    "the entry times" a well-defined object: without it CP-SAT returns an
    arbitrary feasible schedule and there is nothing to compare. Under a pinned
    order the earliest-feasible schedule is a forward pass and is unique, which
    is exactly what _solve_order's weighted objective also drives every entry
    to. Day 10 replaces this line and nothing else.
    """
    prepared: Dict[str, List] = {}
    topologies: Dict[str, Dict] = {}
    for resource_id, (trains_in, topology) in payloads.items():
        prepared[resource_id] = _prepare(trains_in, topology)
        topologies[resource_id] = topology

    headway_s = int(
        next(iter(topologies.values())).get("headway_seconds", DEFAULT_HEADWAY_SECONDS)
        if topologies else DEFAULT_HEADWAY_SECONDS
    )
    max_hold_s = int(
        next(iter(topologies.values())).get("max_hold_seconds", DEFAULT_MAX_HOLD_SECONDS)
        if topologies else DEFAULT_MAX_HOLD_SECONDS
    )

    # Generous enough for the worst legal schedule across every resource.
    # Too small makes the model infeasible for reasons that have nothing to do
    # with the railway; too large costs nothing because entry is minimised.
    all_trains = [t for group in prepared.values() for t in group]
    if not all_trains:
        return GlobalSolution(status="EMPTY", feasible=False)
    horizon = (
        max(t.earliest_arrival_s for t in all_trains)
        + sum(t.occupancy_from_stop_s + headway_s for t in all_trains)
        + max_hold_s
        + 1
    )

    # Forced delay is only a constant once the order is fixed. Unpinned, the
    # anti-starvation ceiling has no per-solve baseline to sit on -- that is
    # precisely the gap total_hold[t] fills on day 8, and until then the bound
    # is the horizon rather than a wrong constant.
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
    in_loop, on_main, wait, delay, interval = {}, {}, {}, {}, {}

    # ---- item 5: one interval per (train, resource) -----------------------
    for resource_id, group in prepared.items():
        for train in group:
            key = (train.train_id, resource_id)
            tag = f"{train.train_id}_{resource_id}"
            ceiling = cap[key]

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

            wait[key] = model.NewIntVar(0, ceiling, f"wait_{tag}")
            model.Add(wait[key] == entry[key] - train.earliest_arrival_s)
            model.Add(wait[key] <= train.absorbable_s).OnlyEnforceIf(
                stopped[key].Not()
            )
            model.Add(wait[key] >= train.absorbable_s + 1).OnlyEnforceIf(
                stopped[key]
            )

            delay[key] = model.NewIntVar(0, ceiling, f"delay_{tag}")
            model.Add(
                delay[key]
                == wait[key]
                + train.loop_stop_penalty_s * in_loop[key]
                + train.main_stop_penalty_s * on_main[key]
            )
            model.Add(delay[key] <= ceiling)

    # ---- item 7: NoOverlap per resource -----------------------------------
    for resource_id, group in prepared.items():
        ivs = [interval[(t.train_id, resource_id)] for t in group]
        if len(ivs) > 1:
            model.AddNoOverlap(ivs)

    # ---- item 6: precedes[i,j,r], ordered, retained ------------------------
    precedes: Dict[Tuple[str, str, str], Any] = {}
    for resource_id, group in prepared.items():
        ids = [t.train_id for t in group]
        for first, second in combinations(ids, 2):
            forward = model.NewBoolVar(f"prec_{first}_{second}_{resource_id}")
            backward = model.NewBoolVar(f"prec_{second}_{first}_{resource_id}")
            precedes[(first, second, resource_id)] = forward
            precedes[(second, first, resource_id)] = backward
            # Exactly one orientation holds. CP-SAT presolves the redundant
            # half away; the pair is declared so day 10 can flip a NAMED
            # variable rather than an implicit orientation.
            model.Add(forward + backward == 1)
            model.Add(
                entry[(second, resource_id)]
                >= exit_[(first, resource_id)] + headway_s
            ).OnlyEnforceIf(forward)
            model.Add(
                entry[(first, resource_id)]
                >= exit_[(second, resource_id)] + headway_s
            ).OnlyEnforceIf(backward)

    for resource_id, order in (pin_order or {}).items():
        position = {train_id: n for n, train_id in enumerate(order)}
        for (first, second, res), var in precedes.items():
            if res != resource_id:
                continue
            if first in position and second in position:
                model.Add(var == (1 if position[first] < position[second] else 0))

    # ---- decision 5: loop capacity across the whole window -----------------
    # Per loop_id, not per conflict. On a single-resource solve this reduces to
    # exactly _solve_order's constraint; across two conflicts it is the case
    # per-conflict solving structurally cannot see.
    loop_intervals: Dict[str, List] = {}
    for resource_id, group in prepared.items():
        for train in group:
            if not train.loop_available or not train.loop_id:
                continue
            key = (train.train_id, resource_id)
            loop_intervals.setdefault(train.loop_id, []).append(
                model.NewOptionalIntervalVar(
                    train.earliest_arrival_s,
                    wait[key],
                    entry[key],
                    in_loop[key],
                    f"loop_{train.train_id}_{resource_id}",
                )
            )
    for ivs in loop_intervals.values():
        if len(ivs) > 1:
            model.AddNoOverlap(ivs)

    # Delay, not entry. delay = wait + stop penalty, and wait = entry -
    # earliest_arrival, so minimising delay still drives every entry to its
    # earliest feasible value under a pinned order -- but it ALSO prices the
    # loop-versus-main choice, which entry alone leaves free. That freedom is
    # not harmless: the stop location changes delay by the gap between the two
    # penalties and CP-SAT picks arbitrarily among equal-entry solutions.
    #
    # Unweighted is sufficient HERE and only here: with the order pinned and
    # entries thereby fixed, each train's stop choice is separable, so every
    # strictly positive weighting selects the same branch _solve_order does.
    # Day 10 replaces this with the lexicographic descent by priority class,
    # at which point the weights start to matter and this line goes away.
    # The SAME objective _solve_order uses -- weight_scaled x tiered cost, plus
    # the tiebreak term -- not a simplification of it.
    #
    # sum(delay) is not a weaker version of this, it is a DIFFERENT objective
    # with different optima. When two trains contend one loop, giving the loop
    # to either costs the same unweighted total (the penalty swap is
    # symmetric), so CP-SAT picks arbitrarily; _solve_order's weights are not
    # indifferent and give it to the higher-weighted train. Reproducing that
    # requires the weights, the tiers, and the tiebreak.
    #
    # Still not the day-10 objective: this is the flat weighted sum, whereas
    # the real rule is a lexicographic descent by priority class. That changes
    # which ORDER gets chosen; under a pinned order the two agree, which is why
    # this is sufficient for the encoding gate and insufficient for day 10.
    objective_terms = []
    for resource_id, group in prepared.items():
        for train in group:
            key = (train.train_id, resource_id)
            first_break, second_break = DELAY_TIER_BREAKS_S
            segment_caps = (
                first_break, second_break - first_break, cap[key]
            )
            segments = [
                model.NewIntVar(0, size, f"seg{n}_{train.train_id}_{resource_id}")
                for n, size in enumerate(segment_caps)
            ]
            model.Add(sum(segments) == delay[key])
            cost = sum(
                multiplier * segment
                for multiplier, segment in zip(DELAY_TIER_MULTIPLIERS, segments)
            )
            objective_terms.append(train.weight_scaled * cost)
            objective_terms.append(TIEBREAK_COEFFICIENT * delay[key])

    model.Minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(SOLVER_TIME_LIMIT_S, 10.0)
    if SOLVER_DETERMINISTIC_TIME > 0:
        solver.parameters.max_deterministic_time = SOLVER_DETERMINISTIC_TIME
    solver.parameters.num_search_workers = SOLVER_WORKERS
    solver.parameters.log_search_progress = solver_log
    status = solver.Solve(model)

    names = {
        cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE", cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    counts = {
        "resources": len(prepared),
        "intervals": len(interval),
        "precedes": len(precedes),
        "loops": len(loop_intervals),
    }
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return GlobalSolution(
            status=names.get(status, str(status)), feasible=False, counts=counts
        )

    solution = GlobalSolution(
        status=names.get(status, str(status)), feasible=True, counts=counts
    )
    for key in interval:
        solution.entry_s[key] = solver.Value(entry[key])
        solution.exit_s[key] = solver.Value(exit_[key])
        solution.wait_s[key] = solver.Value(wait[key])
        solution.delay_s[key] = solver.Value(delay[key])
        solution.stopped[key] = bool(solver.Value(stopped[key]))
        solution.in_loop[key] = bool(solver.Value(in_loop[key]))
        solution.on_main[key] = bool(solver.Value(on_main[key]))
    for triple, var in precedes.items():
        solution.precedes[triple] = solver.Value(var)

    if prepared:
        busiest = max(prepared, key=lambda r: (len(prepared[r]), r))
        ordered = sorted(
            (t.train_id for t in prepared[busiest]),
            key=lambda tid: solution.entry_s[(tid, busiest)],
        )
        if len(ordered) > 1:
            solution.headline = (ordered[0], ordered[1], busiest)

    return solution


def optimize_global(detector, horizon_s: Optional[int] = None):
    """Still a day-8+ deliverable: no objective, no chaining, no directives.

    build_and_solve() above is the days 6-7 encoding and is exercised by
    tests/test_global_encoding.py. This entry point stays raising until the
    lexicographic descent and directive emission exist, because an empty list
    is a valid answer from optimize_precedence and would be indistinguishable
    from "the global engine ran and found nothing".
    """
    raise NotImplementedError(
        "optimize_global needs the day-10 objective and day-11 directive "
        "emission; ENGINE=global is not runnable yet. Use ENGINE=enumerate."
    )