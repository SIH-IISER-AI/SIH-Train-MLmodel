"""Day-5 gate: how big is the global model on scenario10?

Item 4 says stop if the window produces 500+ intervals, because days 6-9 then
spend their time fighting model size instead of encoding. This prints the count
at several horizons so the choice is made against numbers rather than a guess,
and asserts the production horizon is inside the gate.

Also checks the constant that days 6-7 depend on: that detector.project()'s
t_in for a resource agrees with optimizer._prepare()'s earliest_arrival_s for
the same resource. They use the same kinematics module and they must agree, or
the global model prices conflicts on different physics than the one that raised
them -- which is the exact defect F4 fixed on day 1, reintroduced one layer up.

Usage:  python3 tests/count_intervals.py [data/scenario10.json]
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

from detector import ConflictDetector
from injector import LiveTelemetryInjector
from main import solvable_conflicts
from optimizer import _prepare
from optimizer_global import WINDOW_HORIZON_S, scope_window

GATE_INTERVALS = 300
HORIZONS = (900, 1800, 2700, 3600)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
    network = json.load(open("data/network.json"))
    scenario = json.load(open(path))
    fleet = {t["train_id"]: t for t in scenario["trains"]}

    inj = LiveTelemetryInjector(network, scenario)
    det = ConflictDetector(network, fleet)
    for event in inj.tick():
        det.ingest(event)

    print(f"scenario: {path}   detector horizon: {det.horizon_seconds}s")
    print(f"{'horizon':>9} {'intervals':>10} {'resources':>10} "
          f"{'contested':>10} {'precedes':>9} {'largest':>8}")
    for horizon in HORIZONS:
        counts = scope_window(det, horizon).counts()
        flag = "" if counts["intervals"] < GATE_INTERVALS else "   <-- OVER GATE"
        print(f"{horizon:>8}s {counts['intervals']:>10} "
              f"{counts['resources']:>10} {counts['contested_resources']:>10} "
              f"{counts['precedes_ordered']:>9} "
              f"{counts['largest_contention']:>8}{flag}")

    scope = scope_window(det, WINDOW_HORIZON_S)
    counts = scope.counts()
    print(f"\ncontested resources at the production horizon "
          f"({WINDOW_HORIZON_S}s)")
    for resource_id, group in sorted(
        scope.contested.items(), key=lambda kv: -len(kv[1])
    ):
        trains = sorted({i.train_id for i in group})
        line = "single" if group[0].single_line else "double"
        print(f"  {resource_id:<26} {len(trains):>2} trains  {line}  "
              f"{' '.join(trains)}")
        
            # Contiguity. Chaining assumes a train's projected occupancies are DISJOINT
    # AND SEQUENTIAL -- resource k+1 begins exactly where k ends. project() sets
    # distance = position.resource_end_km each step and topology.resolve()
    # returns exactly one resource per position, so this should hold by
    # construction; day-2 output confirms it (12626 exits BLK-115D at 611 s and
    # enters SEC-PWL-KSV at 611 s). Assert it anyway: if project() ever develops
    # a gap or an overlap, chaining encodes it silently and the model quietly
    # prices a schedule no train can run.
    #
    # Tolerance is 1 s and the reason is nameable: earliest_in_s is ceil(t_in)
    # and running_s is ceil(t_out) - ceil(t_in), so the identity is exact in
    # real seconds and can only differ by the rounding step. Anything larger is
    # a real discontinuity.
    print("\ncontiguity: intervals[k].earliest_in == intervals[k-1].earliest_in "
          "+ intervals[k-1].running")
    breaks = 0
    worst_gap = 0
    for train_id, group in sorted(scope.by_train.items()):
        for previous, following in zip(group, group[1:]):
            gap = abs(
                previous.earliest_in_s + previous.running_s - following.earliest_in_s
            )
            worst_gap = max(worst_gap, gap)
            if gap > 1:
                breaks += 1
                print(f"  BREAK {train_id} {previous.resource_id} -> "
                      f"{following.resource_id}: {previous.earliest_in_s}"
                      f"+{previous.running_s} != {following.earliest_in_s} "
                      f"({gap}s)")
    print(f"  worst gap: {worst_gap}s across "
          f"{sum(max(0, len(g) - 1) for g in scope.by_train.values())} hops")

    # Physics parity. Take the conflict evaluate() would solve and compare
    # _prepare's earliest_arrival_s against project()'s t_in for the same
    # (train, resource). A drift here means days 6-9 build on constants that
    # disagree with the detector that raised the alert.
    print("\nphysics parity: _prepare.earliest_arrival_s vs project().t_in")
    solved, _counts = solvable_conflicts(det)
    worst = 0
    for group in solved.values():
        resource_id = group["resource_id"]
        payload_trains, payload_topology = det.optimiser_inputs(group)
        prepared = {p.train_id: p for p in _prepare(payload_trains, payload_topology)}
        for interval in scope.by_resource.get(resource_id, []):
            candidate = prepared.get(interval.train_id)
            if candidate is None:
                continue
            drift = abs(candidate.earliest_arrival_s - interval.earliest_in_s)
            worst = max(worst, drift)
            if drift > 2:
                print(f"  DRIFT {interval.train_id} on {resource_id}: "
                      f"_prepare={candidate.earliest_arrival_s}s "
                      f"project={interval.earliest_in_s}s ({drift}s)")
    print(f"  worst drift: {worst}s")

    print()
    if breaks:
        print(f"GATE FAILED: {breaks} projection discontinuit(ies). project() "
              f"has a gap or an overlap; chaining would encode it silently.")
        return 1
    if counts["intervals"] >= GATE_INTERVALS:
        print(f"GATE FAILED: {counts['intervals']} intervals at "
              f"{WINDOW_HORIZON_S}s, ceiling is {GATE_INTERVALS}. Narrow the "
              f"horizon before starting day 6.")
        return 1
    print(f"GATE PASSED: {counts['intervals']} intervals, "
          f"{counts['precedes_ordered']} precedence booleans, largest "
          f"contention {counts['largest_contention']} trains.")
    print(f"  For scale: enumerate caps that contention at 5 trains and still "
          f"solves 120 models. The global model carries the full "
          f"{counts['largest_contention']} in "
          f"{counts['largest_contention'] * (counts['largest_contention'] - 1)} "
          f"booleans on that resource.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())