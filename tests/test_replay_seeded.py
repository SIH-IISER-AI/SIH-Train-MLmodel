"""Rows 1.5 and 1.6: replay coverage and ordered-train positivity, seeded.

test_directive_replay.py asserts both at tick 1 on the unperturbed scenario.
A run is 1080 ticks and the stand-impossible degradation fires throughout --
2,791 times on seed 3 alone -- so a tick-1 assertion is a boot-state gate
wearing an n=15 name. This samples every seed at first conflict and again
mid-run.

Perturbation comes from harness.build_injector, never reimplemented. Two
day-2 defects came from measurement tooling re-deriving a production rule
slightly differently and being masked by a tick where the rules agreed;
solvable_conflicts was extracted from evaluate() for the same reason.

1.5  every train the model priced a hold for receives a directive, 0 misses
1.6  observed standing minus priced hold >= 0 for every ordered train

1.6 is asserted only on trains ordered to STAND. A train carrying priced
standing that receives a REGULATE is compared against a quantity it was
never given, which is how 12138 read -5119 on day 13.
"""
import json
import os
import sys

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")
sys.path.insert(0, "tests")

from detector import ConflictDetector
from main import solvable_conflicts
from optimizer_global import optimize_global
from harness import build_injector, NETWORK_PATH, SCENARIO_PATH

SEEDS = [int(s) for s in os.getenv(
    "REPLAY_SEEDS", " ".join(str(i) for i in range(1, 16))).split()]
SAMPLE_TICKS = [int(t) for t in os.getenv("REPLAY_SAMPLES", "1 540").split()]
WARMUP_TICKS = 200
#: Derived from the plan, not fixed -- the same rule test_directive_replay.py
#: uses. A train the model holds for 14,700 s is still standing when a 9,000 s
#: watch ends, and a fixed window then measures the watch rather than the plan.
WATCH_MARGIN_S = float(os.getenv("REPLAY_WATCH_MARGIN_S", "3600"))
MIN_WATCH_S = float(os.getenv("REPLAY_MIN_WATCH_S", "9000"))
STANDING_KMH = 1.0

fail = []


def sample(seed, from_tick):
    network = json.load(open(NETWORK_PATH))
    base = json.load(open(SCENARIO_PATH))
    inj, scenario, _ = build_injector(base, network, seed)
    det = ConflictDetector(network, {t["train_id"]: t for t in scenario["trains"]})

    candidates = {}
    for tick in range(1, from_tick + WARMUP_TICKS + 1):
        for event in inj.tick():
            det.ingest(event)
        if tick < from_tick:
            continue
        candidates, _ = solvable_conflicts(det)
        if candidates:
            break
    if not candidates:
        return f"seed {seed} t{from_tick}: no conflict", None

    plans = optimize_global(det, candidates)
    if not plans:
        return f"seed {seed} t{from_tick}: no plan", None

    directives, seen, priced = [], set(), {}
    for scenarios in plans.values():
        best = scenarios[0]
        for record in best["delay_breakdown"]:
            priced[record["train_id"]] = record["cumulative_hold_seconds"]
        for directive in best["directives"]:
            key = (directive["train_id"], directive["kind"],
                   directive.get("station_id"))
            if key in seen:
                continue
            seen.add(key)
            directives.append(directive)

    # -- 1.5 coverage --
    targeted = {d["train_id"] for d in directives}
    held = {t for t, hold in priced.items() if hold > 0}
    missing = sorted(held - targeted)

    # -- 1.6 positivity --
    ordered = {d["train_id"] for d in directives if d["kind"] != "REGULATE"}
    for directive in directives:
        inj.submit_directive(directive)
    inj.tick()

    watch_s = max(MIN_WATCH_S, max(priced.values(), default=0) + WATCH_MARGIN_S)
    stood = {tid: 0.0 for tid in inj.trains}
    start = inj.elapsed_sim_seconds
    while inj.elapsed_sim_seconds - start < watch_s:
        inj.tick()
        step = inj.sim_seconds_per_tick
        for tid, train in inj.trains.items():
            if train.speed_kmh < STANDING_KMH:
                stood[tid] += step

    negative = sorted(
        (tid, int(stood[tid]) - int(priced.get(tid, 0)))
        for tid in ordered
        if int(stood[tid]) - int(priced.get(tid, 0)) < 0
    )
    return None, (len(directives), len(held), missing, len(ordered),
                  negative, watch_s)


print(f"seeds {SEEDS}   sample ticks {SAMPLE_TICKS}   "
      f"watch >= {MIN_WATCH_S:.0f}s, plan + {WATCH_MARGIN_S:.0f}s\n")
print("seed  tick  dirs  priced  missing  ordered  negative")
for seed in SEEDS:
    for from_tick in SAMPLE_TICKS:
        skip, result = sample(seed, from_tick)
        if skip:
            print(f"  {seed:>3} {from_tick:>5}   -- {skip}")
            continue
        n_dir, n_held, missing, n_ord, negative, watch_s = result
        print(f"  {seed:>3} {from_tick:>5} {n_dir:>5} {n_held:>7} "
              f"{len(missing):>8} {n_ord:>8} {len(negative):>9}"
              f"  watch {result[5]:.0f}s")
        for train_id in missing:
            msg = (f"1.4 seed {seed} t{from_tick}: {train_id} priced a hold, "
                   f"no directive")
            print(f"        {msg}")
            fail.append(msg)
        for train_id, delta in negative:
            msg = (f"1.5 seed {seed} t{from_tick}: {train_id} ordered to "
                   f"stand, observed minus priced = {delta}")
            print(f"        {msg}")
            fail.append(msg)

print()
if fail:
    print("REPLAY-FAIL: " + "; ".join(fail[:12]))
    sys.exit(1)
print("REPLAY-PASS")
