"""Day-11 gate: the descent completes, and completes the same way twice.

Two properties, both learned the hard way:

  1. All tiers run. With the budget divided across tiers instead of allocated
     per tier, the descent completed 1 of 6 at every total from 0.5 s to 2.0 s.
     worst_hold is tier 4, so it never executed, and 12280 carried 22,707 s of
     standing. The returned status was tier 0's -- OPTIMAL -- so nothing said
     anything was wrong.

  2. The same input gives the same plan. Below ~1.2 s per tier the descent
     degrades non-deterministically: same budget, different tier counts,
     different orders. That is the finding we hold against enumerate above
     cap 6, and it must not be true of us.

If this fails, someone lowered a budget or added a tier. Better here than in
a demo.

Usage:  python3 tests/test_descent.py [scenario.json]
"""
from __future__ import annotations

import sys

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

from detector import ConflictDetector
from injector import LiveTelemetryInjector
from main import solvable_conflicts
from optimizer_global import GLOBAL_STARVATION_THRESHOLD_S, solve_with_policy

import json

failures = []


def check(what, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got} (want {want})")
    if not ok:
        failures.append(what)


def payloads_at_tick(scenario_path: str):
    network = json.load(open("data/network.json"))
    scenario = json.load(open(scenario_path))
    injector = LiveTelemetryInjector(network, scenario)
    detector = ConflictDetector(
        network, {t["train_id"]: t for t in scenario["trains"]}
    )
    for tick in range(1, 121):
        for event in injector.tick():
            detector.ingest(event)
        candidates, _ = solvable_conflicts(detector)
        if candidates:
            break
    built = {}
    for conflict in candidates.values():
        resource_id = conflict["resource_id"]
        if resource_id in built:
            continue
        trains_in, topology = detector.optimiser_inputs(conflict)
        if len(trains_in) >= 2:
            built[resource_id] = (trains_in, topology)
    return tick, built


def main() -> int:
    scenario_path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
    tick, payloads = payloads_at_tick(scenario_path)
    print(f"scenario: {scenario_path}   first conflict at tick {tick}   "
          f"contested resources: {len(payloads)}\n")
    if not payloads:
        print("FAIL: no payloads")
        return 1

    runs = []
    for n in (1, 2):
        solution = solve_with_policy(payloads)
        counts = solution.counts
        print(f"run {n}: {solution.status}  "
              f"tiers={counts.get('tiers_completed')}/{counts.get('tiers_total')}  "
              f"truncated={counts.get('truncated')}  "
              f"worst_hold={counts.get('worst_hold_s')}s")
        print(f"        {counts.get('tier_log')}")
        runs.append(solution)

    first, second = runs
    check("run 1 feasible", first.feasible, True)
    check("run 2 feasible", second.feasible, True)
    if not (first.feasible and second.feasible):
        print("\nFAIL: " + "; ".join(failures))
        return 1

    for n, solution in enumerate(runs, start=1):
        counts = solution.counts
        check(f"run {n} completed every tier",
              counts.get("tiers_completed"), counts.get("tiers_total"))
        check(f"run {n} not truncated", counts.get("truncated"), 0)

    # Determinism. The holds are the sharpest witness: they are the last tier's
    # output, so if any tier above resolved differently they differ too.
    check("both runs produced the same holds",
          first.total_hold_s, second.total_hold_s)
    check("both runs produced the same precedence",
          first.precedes, second.precedes)
    check("both runs chose the same headline",
          first.headline, second.headline)

    worst = max(first.total_hold_s.values(), default=0)
    flagged = worst > GLOBAL_STARVATION_THRESHOLD_S
    print(f"\nworst hold {worst}s against a "
          f"{GLOBAL_STARVATION_THRESHOLD_S}s guideline")
    check("policy_exceeded agrees with the threshold",
          first.policy_exceeded, flagged)
    if flagged:
        print(f"  starved: {first.counts.get('starved')} "
              f"-- the card says so, which is the point")

    print("\nFAIL: " + "; ".join(failures[:12]) if failures else "\nPASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())