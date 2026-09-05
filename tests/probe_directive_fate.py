import json, os, sys
sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")
sys.path.insert(0, "tests")
from detector import ConflictDetector
from main import solvable_conflicts
from optimizer_global import optimize_global
from harness import build_injector, NETWORK_PATH, SCENARIO_PATH

SEED = int(os.getenv("SEED", "3"))
TICKS = int(os.getenv("TICKS", "1080"))

network = json.load(open(NETWORK_PATH))
base = json.load(open(SCENARIO_PATH))
inj, scenario, _ = build_injector(base, network, SEED)
det = ConflictDetector(network, {t["train_id"]: t for t in scenario["trains"]})

emitted = 0
queued = 0
rejected_at_queue = 0
approved_conflicts = set()

for tick in range(1, TICKS + 1):
    for event in inj.tick():
        det.ingest(event)
    candidates, _ = solvable_conflicts(det)
    if not candidates:
        continue
    plans = optimize_global(det, candidates, max_scenarios=1)
    for conflict_id, scenarios in plans.items():
        if conflict_id in approved_conflicts:
            continue
        approved_conflicts.add(conflict_id)
        for directive in scenarios[0].get("directives", []):
            emitted += 1
            if inj.submit_directive(directive):
                queued += 1
            else:
                rejected_at_queue += 1

applied = len(inj.applied_directives)
refused = sum(inj.refused_directives.values())

print(f"seed {SEED}, {TICKS} ticks")
print(f"  conflicts approved            {len(approved_conflicts)}")
print(f"  directives emitted            {emitted}")
print(f"  rejected at submit()          {rejected_at_queue}")
print(f"  queued                        {queued}")
print(f"  applied                       {applied}")
print(f"  refused in _drain_directives  {refused}")
print(f"  re-targeted (applied, but not where asked)  {inj.retargeted_directives}")
print()
for reason in sorted(inj.refused_directives):
    print(f"    {reason:34} {inj.refused_directives[reason]:6d}")
print()
still_pending = len(inj._pending)
print(f"IDENTITY  applied + refused == queued - still_pending")
print(f"  {applied} + {refused} = {applied + refused}   "
      f"vs {queued} - {still_pending} = {queued - still_pending}")
print(f"  HOLDS: {applied + refused == queued - still_pending}")
if applied > 0:
    print(f"\nexecution rate  {applied}/{queued} = {applied / queued:.1%}")
    print(f"as instructed   {applied - inj.retargeted_directives}/{queued} = "
          f"{(applied - inj.retargeted_directives) / queued:.1%}")