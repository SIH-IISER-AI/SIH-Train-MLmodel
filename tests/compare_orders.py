"""Day-10 measurement: the unpinned global order against enumerate's OPT-1.

This comparison was correctly deferred on day 7. Before chaining and before the
per-train convex tier, a divergence had too many possible causes to interpret.
It is legitimate now: the encoding is gated exactly against _solve_order in the
isolated case, the descent completes 6/6 tiers deterministically, and any
remaining difference is either coordination the per-conflict engine cannot see
or a real disagreement about precedence.

This is a MEASUREMENT, not a pass/fail gate. Divergence is the expected and
desired outcome -- if the global model always agreed with enumerate there would
be no reason to build it. What the output has to support is a claim about WHY
each divergence happens, resource by resource.

Usage:  python3 tests/compare_orders.py [scenario.json]
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
from optimizer import MAX_TRAINS_ENUMERATED, optimize_precedence
from optimizer_global import solve_with_policy

WARMUP_TICKS = 200


def main() -> int:
    scenario_path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
    network = json.load(open("data/network.json"))
    scenario = json.load(open(scenario_path))

    injector = LiveTelemetryInjector(network, scenario)
    detector = ConflictDetector(
        network, {t["train_id"]: t for t in scenario["trains"]}
    )
    candidates = {}
    for tick in range(1, WARMUP_TICKS + 1):
        for event in injector.tick():
            detector.ingest(event)
        candidates, _ = solvable_conflicts(detector)
        if candidates:
            break
    if not candidates:
        print("SKIP: no conflict within warmup")
        return 0

    payloads, order_enum, members = {}, {}, {}
    for conflict in candidates.values():
        resource_id = conflict["resource_id"]
        if resource_id in payloads:
            continue
        trains_in, topology = detector.optimiser_inputs(conflict)
        if len(trains_in) < 2:
            continue
        payloads[resource_id] = (trains_in, topology)
        members[resource_id] = [t["train_id"] for t in trains_in]
        scenarios = optimize_precedence(trains_in, topology)
        order_enum[resource_id] = (
            list(scenarios[0]["order_train_ids"]) if scenarios else []
        )

    print(f"scenario: {scenario_path}   tick {tick}   "
          f"contested resources: {len(payloads)}\n")

    solution = solve_with_policy(payloads)
    counts = solution.counts
    print(f"global: {solution.status}  "
          f"tiers={counts.get('tiers_completed')}/{counts.get('tiers_total')}  "
          f"truncated={counts.get('truncated')}")
    if counts.get("truncated"):
        print("  WARNING: the descent truncated. A truncated descent is not the\n"
              "  lexicographic optimum and this comparison is not valid against\n"
              "  one. Raise GLOBAL_TIER_BUDGET_S and re-run.")
    print()

    agree = diverge = 0
    for resource_id in sorted(payloads):
        ids = members[resource_id]
        enum_order = order_enum[resource_id]
        glob_order = sorted(
            ids, key=lambda t: (solution.entry_s[(t, resource_id)], t)
        )
        dropped = len(ids) - len(enum_order)
        # Enumerate compares only the subset its cap admits; comparing the full
        # global order against a truncated one would count the cap itself as a
        # divergence, which is a separate finding and already measured.
        glob_subset = [t for t in glob_order if t in set(enum_order)]
        same = glob_subset == enum_order
        agree += int(same)
        diverge += int(not same)

        print(f"{resource_id}  ({len(ids)} trains"
              f"{f', enumerate dropped {dropped}' if dropped else ''})")
        print(f"  enumerate OPT-1 : {' -> '.join(enum_order) or '(none)'}")
        print(f"  global unpinned : {' -> '.join(glob_order)}")
        if same:
            print("  AGREE on the enumerated subset")
        else:
            moved = [
                t for n, t in enumerate(glob_subset)
                if n < len(enum_order) and t != enum_order[n]
            ]
            print(f"  DIVERGE: {', '.join(moved) or 'ordering differs'}")
            for train_id in set(enum_order) | set(glob_subset):
                key = (train_id, resource_id)
                if key in solution.slack_s:
                    print(f"      {train_id}: slack {solution.slack_s[key]}s  "
                          f"total_hold {solution.total_hold_s.get(train_id, 0)}s  "
                          f"stopped={solution.stopped[key]}")
        print()

    print(f"agree {agree} / diverge {diverge} of {len(payloads)} resources")
    print(f"enumerate cap: {MAX_TRAINS_ENUMERATED} trains")
    print("\nEach divergence needs a sentence before it goes on a slide. The two\n"
          "legitimate causes are (a) a train's order here is constrained by its\n"
          "chained arrival from another resource, and (b) loop capacity couples\n"
          "two resources the per-conflict engine solves separately. Anything\n"
          "else is worth investigating, not reporting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())