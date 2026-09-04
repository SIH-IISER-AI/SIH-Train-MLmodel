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

totals = {"cards": 0, "empty_directives": 0, "empty_clauses": 0, "both": 0,
          "clear_only": 0, "actionable": 0}
offenders = []

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
        for conflict_id, scenarios in plans.items():
            best = scenarios[0]
            n_dir = len(best.get("directives", []))
            action = (best.get("action") or "").strip()
            n_cla = len([c for c in action.split(";") if c.strip()])
            totals["cards"] += 1
            if n_dir == 0:
                totals["empty_directives"] += 1
            if n_cla == 0:
                totals["empty_clauses"] += 1
            if n_dir == 0 and n_cla > 0:
                totals["both"] += 1
                verbs = [c.strip().split()[0].lower()
                         for c in action.split(";") if c.strip()]
                if all(v == "clear" for v in verbs):
                    totals["clear_only"] += 1
                else:
                    totals["actionable"] += 1
                    offenders.append((seed, from_tick, conflict_id, n_cla,
                                      action[:70]))

print(f"cards                              {totals['cards']}")
print(f"cards with 0 directives            {totals['empty_directives']}")
print(f"cards with 0 clauses               {totals['empty_clauses']}")
print(f"GATE 1.6 violations (clauses>0,")
print(f"  directives==0)                   {totals['both']}")
print(f"  of which clear-only (benign)      {totals['clear_only']}")
print(f"  of which actionable (the defect)  {totals['actionable']}")
print()
for seed, tick, cid, n, head in offenders[:25]:
    print(f"  seed {seed:>2} t{tick:<4} {cid:<28} clauses={n:<3} {head}")
if totals["both"]:
    print(f"\nGATE-1.6-FAIL: {totals['both']} of {totals['cards']} cards")
    sys.exit(1)
print("\nGATE-1.6-PASS")