import json, os, sys
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

buckets = {"with_directives": 0, "has_uninstructed": 0,
           "covered_only": 0, "benign": 0}
covered_rows = []
bad_action = []
ghost = []
train_12001 = []

for seed in SEEDS:
    for from_tick in SAMPLE_TICKS:
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
            continue
        plans = optimize_global(det, candidates)
        if not plans:
            continue
        instructed = set()
        for scenarios in plans.values():
            for d in scenarios[0].get("directives", []):
                instructed.add(d["train_id"])
        for conflict_id, scenarios in plans.items():
            best = scenarios[0]
            action = (best.get("action") or "").strip()
            unins = best.get("uninstructed_train_ids", [])
            cov = best.get("covered_elsewhere", [])
            if best.get("directives"):
                buckets["with_directives"] += 1
                continue
            if unins:
                buckets["has_uninstructed"] += 1
                if "12001" in unins:
                    train_12001.append((seed, from_tick, conflict_id))
                continue
            if cov:
                buckets["covered_only"] += 1
                if action != "":
                    bad_action.append((seed, from_tick, conflict_id, action[:50]))
                for c in cov:
                    covered_rows.append((seed, from_tick, conflict_id,
                                         c["train_id"], c["kind"],
                                         c["motivating_resource_id"],
                                         c["priced_resource_id"]))
                    if c["train_id"] not in instructed:
                        ghost.append((seed, from_tick, conflict_id, c["train_id"]))
                continue
            buckets["benign"] += 1

total = sum(buckets.values())
print(f"cards                     {total}")
for name in ("with_directives", "has_uninstructed", "covered_only", "benign"):
    print(f"  {name:22} {buckets[name]}")
print()
print("RECONCILE vs gate: covered_only must be 28, has_uninstructed 33, benign 22")
print(f"  covered_only == 28        {buckets['covered_only'] == 28}")
print(f"  has_uninstructed == 33    {buckets['has_uninstructed'] == 33}")
print(f"  benign == 22              {buckets['benign'] == 22}")
print()
print(f"covered-only cards with a non-empty action  {len(bad_action)}  (must be 0)")
for row in bad_action[:10]:
    print("   ", row)
print(f"GHOST cover claims (train has no directive) {len(ghost)}  (must be 0)")
for row in ghost[:10]:
    print("   ", row)
print()
print(f"covered_elsewhere entries: {len(covered_rows)}")
for seed, tick, cid, tid, kind, mot, priced in covered_rows[:40]:
    print(f"  seed {seed:>2} t{tick:<4} {cid:<28} {tid:<7} {kind:<14} "
          f"mot={mot} priced={priced}")
print()
print(f"cards where 12001 is uninstructed: {len(train_12001)}")
for row in train_12001[:15]:
    print("   ", row)