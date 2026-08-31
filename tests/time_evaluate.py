#!/usr/bin/env python3
"""Day 12 pre-flight: wall-clock cost of one ENGINE=global evaluate."""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

os.environ.setdefault("ENGINE", "global")

from detector import ConflictDetector                        # noqa: E402
from injector import LiveTelemetryInjector                   # noqa: E402
from main import solvable_conflicts                          # noqa: E402
from optimizer_global import optimize_global, solve_with_policy  # noqa: E402

NETWORK_PATH = os.getenv("NETWORK_PATH", "data/network.json")
SCENARIO_PATH = os.getenv("SCENARIO_PATH", "data/scenario10.json")
TICKS = int(os.getenv("PROBE_TICKS", "1"))
REPEATS = int(os.getenv("PROBE_REPEATS", "3"))


def boot():
    network = json.load(open(NETWORK_PATH))
    scenario = json.load(open(SCENARIO_PATH))
    inj = LiveTelemetryInjector(
        network=network,
        scenario=scenario,
        tick_seconds=float(os.getenv("TICK_SECONDS", "2.0")),
        time_multiplier=int(os.getenv("TIME_MULTIPLIER", "5")),
    )
    fleet = {t["train_id"]: t for t in scenario["trains"]}
    det = ConflictDetector(network, fleet)
    for _ in range(TICKS):
        for event in inj.tick():
            det.ingest(event)
    return det


def main() -> None:
    print(f"scenario {SCENARIO_PATH}  ticks {TICKS}")
    for name in ("GLOBAL_TIER_BUDGET_S", "GLOBAL_DET_BUDGET",
                 "GLOBAL_MAX_STOPS", "GLOBAL_HOLD_CAP_MULTIPLIER"):
        print(f"  {name}={os.getenv(name, '<default>')}")

    det = boot()
    candidates, counts = solvable_conflicts(det)
    print(f"conflicts raw={counts['raw']} horizon={counts['within_horizon']} "
          f"actionable={counts['actionable']} distinct={counts['distinct']}")

    floor_kmh = 5.0
    print(f"\n--- T-minus vs projection floor ({floor_kmh} km/h) ---")
    for conflict_id in sorted(candidates):
        conflict = candidates[conflict_id]
        tminus = conflict["predicted_time_to_conflict_seconds"]
        members, _topo = det.optimiser_inputs(conflict)
        implied_m = tminus * floor_kmh / 3.6
        print(f"{conflict_id:>14}  T-{tminus:>5}s  implied {implied_m:>8.0f} m  "
              f"res={conflict['resource_id']}  n={len(members)}")
        for t in sorted(members, key=lambda t: -t["distance_to_bottleneck"]):
            d = t["distance_to_bottleneck"]
            ratio = d / implied_m if implied_m else 0.0
            print(f"{'':>16}  {t['train_id']:>8}  raw {t['current_speed']:>6.1f} km/h  "
                  f"d {d:>8.0f} m  ratio {ratio:>5.2f}")

    for run in range(1, REPEATS + 1):
        started = time.perf_counter()
        plans = optimize_global(det, candidates)
        wall = time.perf_counter() - started
        with_opt2 = sum(1 for s in plans.values() if len(s) > 1)
        empty = sum(1 for s in plans.values() if not s[0].get("directives"))
        print(f"run {run}: evaluate {wall:7.2f}s  cards={len(plans)}  "
              f"cards_with_OPT2={with_opt2}  empty_cards={empty}")
        if run == 1:
            for cid, opts in sorted(plans.items()):
                print(f"    {cid} opts={len(opts)} "
                      f"directives={len(opts[0].get('directives', []))} "
                      f"policy_exceeded={opts[0].get('policy_exceeded')}")

    started = time.perf_counter()
    optimize_global(det, candidates, max_scenarios=1)
    print(f"evaluate (max_scenarios=1): {time.perf_counter() - started:7.2f}s")
    payloads = {}
    for conflict in candidates.values():
        resource_id = conflict["resource_id"]
        if resource_id in payloads:
            continue
        trains_in, topology = det.optimiser_inputs(conflict)
        if len(trains_in) >= 2:
            payloads[resource_id] = (trains_in, topology)

    started = time.perf_counter()
    solution = solve_with_policy(payloads, objective="lexicographic")
    wall = time.perf_counter() - started
    print(f"single descent: {wall:7.2f}s  feasible={solution.feasible}  "
          f"policy_exceeded={solution.policy_exceeded}")
    print(f"  counts={solution.counts}")


if __name__ == "__main__":
    main()