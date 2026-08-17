"""Measure what F5 actually bought. Run from the repo root.

    python3 tests/bench_solve.py

Times the full permutation enumeration on every conflict the current scenario
produces, under four (workers, per-solve limit) combinations: the old settings,
the new settings, and both crosses. The cross terms are the point -- they tell
you whether the win came from dropping to one worker or from the shorter limit,
and "we changed two things and it got faster" is not a measurement.
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

REPEATS = 5
WARMUP = 1
TICKS = 30

network = json.load(open("data/network.json"))
scenario = json.load(open("data/scenario.json"))
fleet = {t["train_id"]: t for t in scenario["trains"]}

inj = LiveTelemetryInjector(network, scenario)
det = ConflictDetector(network, fleet)
for _ in range(TICKS):
    for ev in inj.tick():
        det.ingest(ev)

conflicts = det.detect_grouped()
if not conflicts:
    print("No conflicts at tick %d -- nothing to benchmark." % TICKS)
    raise SystemExit(1)

payloads = []
for conflict in conflicts:
    trains_in, topo = det.optimiser_inputs(conflict)
    if len(trains_in) >= 2:
        payloads.append((conflict["resource_id"], trains_in, topo))

print(f"scenario: {len(fleet)} trains, {len(payloads)} solvable conflicts "
      f"at tick {TICKS}")
for resource_id, trains_in, _ in payloads:
    print(f"  {resource_id:24} group={len(trains_in)} "
          f"enumerated={min(len(trains_in), optimizer.MAX_TRAINS_ENUMERATED)} "
          f"dropped={max(0, len(trains_in) - optimizer.MAX_TRAINS_ENUMERATED)}")
print()

ARMS = [
    ("old   workers=4 limit=2.00", 4, 2.00),
    ("cross workers=4 limit=0.25", 4, 0.25),
    ("cross workers=1 limit=2.00", 1, 2.00),
    ("new   workers=1 limit=0.25", 1, 0.25),
]

print(f"{'arm':28} {'median':>9} {'min':>9} {'max':>9}   per-conflict medians")
print("-" * 92)

for label, workers, limit in ARMS:
    optimizer.SOLVER_WORKERS = workers
    optimizer.SOLVER_TIME_LIMIT_S = limit

    per_conflict = []
    for _resource_id, trains_in, topo in payloads:
        samples = []
        for run in range(WARMUP + REPEATS):
            start = time.perf_counter()
            optimizer.optimize_precedence(trains_in, topo)
            elapsed = time.perf_counter() - start
            if run >= WARMUP:
                samples.append(elapsed)
        per_conflict.append(statistics.median(samples))

    detail = "  ".join(f"{v * 1000:.0f}ms" for v in per_conflict)
    print(f"{label:28} {statistics.median(per_conflict) * 1000:8.1f}ms "
          f"{min(per_conflict) * 1000:8.1f}ms {max(per_conflict) * 1000:8.1f}ms   {detail}")

print()
print("Slowest single conflict under the NEW settings is the number that goes")
print("in the merge gate. If old and new are within noise, the two minutes you")
print("were chasing are somewhere other than the solver -- find out where before")
print("you spend a week rewriting it.")