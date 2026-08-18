"""Measure how enumeration cost grows with MAX_TRAINS_ENUMERATED.

    python3 tests/bench_cap_sweep.py data/scenario10.json

Raises the cap one step at a time on the largest contention group and times a
full enumeration at each. Disables ENUMERATION_BUDGET_S for the duration --
with the budget in place the sweep measures the budget, not the engine, and
flattens into a false plateau at cap 7.
"""

import json
import statistics
import sys
import time

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

import optimizer  # noqa: E402
from detector import ConflictDetector  # noqa: E402
from injector import LiveTelemetryInjector  # noqa: E402

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
CAPS = [int(c) for c in sys.argv[2].split(",")] if len(sys.argv) > 2 else [4, 5, 6, 7, 8]
TICKS = 1
REPEATS = 3
ABORT_OVER_S = 600.0

network = json.load(open("data/network.json"))
scenario = json.load(open(SCENARIO))
fleet = {t["train_id"]: t for t in scenario["trains"]}

inj = LiveTelemetryInjector(network, scenario)
det = ConflictDetector(network, fleet)
for _ in range(TICKS):
    for ev in inj.tick():
        det.ingest(ev)

groups = det.detect_grouped()
if not groups:
    print(f"No conflicts in {SCENARIO} at tick {TICKS}.")
    raise SystemExit(1)

biggest = max(groups, key=lambda g: len(g["conflicting_train_ids"]))
trains_in, topo = det.optimiser_inputs(biggest)
print(f"{SCENARIO}: largest group {biggest['resource_id']} "
      f"with {len(trains_in)} trains\n")

# The budget would truncate every arm above cap 6 at the same 5 s and make the
# growth curve look like it flattens. Restore it afterwards -- other code in
# this process may still call the solver.
saved_budget = optimizer.ENUMERATION_BUDGET_S
optimizer.ENUMERATION_BUDGET_S = float(ABORT_OVER_S * 10)
saved_cap = optimizer.MAX_TRAINS_ENUMERATED

print(f"{'cap':>4} {'trains':>7} {'perms':>9} {'median':>11} {'per-solve':>11}")
print("-" * 48)
try:
    for cap in CAPS:
        if cap > len(trains_in):
            print(f"{cap:>4} {'--':>7} {'--':>9}   group only has {len(trains_in)}")
            continue
        optimizer.MAX_TRAINS_ENUMERATED = cap
        perms = 1
        for k in range(2, cap + 1):
            perms *= k

        samples = []
        for _ in range(REPEATS):
            start = time.perf_counter()
            optimizer.optimize_precedence(trains_in, topo)
            samples.append(time.perf_counter() - start)
            if samples[-1] > ABORT_OVER_S:
                break
        median = statistics.median(samples)
        print(f"{cap:>4} {len(trains_in):>7} {perms:>9} "
              f"{median:>10.2f}s {median / perms * 1000:>10.2f}ms")
        if median > ABORT_OVER_S:
            print(f"     stopping: cap {cap} already exceeds {ABORT_OVER_S:.0f}s")
            break
finally:
    optimizer.ENUMERATION_BUDGET_S = saved_budget
    optimizer.MAX_TRAINS_ENUMERATED = saved_cap

print("\nExtrapolate the remaining caps from the per-solve column x n!.")
print("Report the measured points and the extrapolation separately.")