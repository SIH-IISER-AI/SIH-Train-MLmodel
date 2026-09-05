import json, os, sys
sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")
from detector import ConflictDetector
from injector import LiveTelemetryInjector
from optimizer_global import build_and_solve

SCENARIO = os.getenv("SCENARIO_PATH", "data/scenario10.json")
RESOURCE = os.getenv("PROBE_RESOURCE", "TRK-DOWN-MAIN|BLK-108D")
PIN = os.getenv("PROBE_PIN", "12050 20172").split()
FORCED = os.getenv("PROBE_FORCED", "12050")
CEILING_TRAIN = os.getenv("PROBE_CEILING_TRAIN", "20172")
CEILING_S = int(os.getenv("PROBE_CEILING_S", "245"))
OBJECTIVE = os.getenv("PROBE_OBJECTIVE", "flat")

network = json.load(open("data/network.json"))
scenario = json.load(open(SCENARIO))
fleet = {t["train_id"]: t for t in scenario["trains"]}
inj = LiveTelemetryInjector(network, scenario)
det = ConflictDetector(network, fleet)

conflicts = []
for tick in range(1, 121):
    for event in inj.tick():
        det.ingest(event)
    conflicts = det.detect_grouped()
    if conflicts:
        break
if not conflicts:
    print("SKIP: no conflict within 120 ticks")
    sys.exit(0)
print(f"scenario {SCENARIO}   first conflict at tick {tick}   "
      f"{len(conflicts)} conflicts")
print("resources: " + ", ".join(sorted({c['resource_id'] for c in conflicts})))

target = next((c for c in conflicts if c["resource_id"] == RESOURCE), None)
if target is None:
    print(f"\nFAIL: {RESOURCE} not among the conflicts at this tick")
    sys.exit(1)

trains_in, topology = det.optimiser_inputs(target)
available = [t["train_id"] for t in trains_in]
print(f"\n{RESOURCE}: {len(trains_in)} trains -> {' '.join(available)}")
missing = [t for t in PIN if t not in available]
if missing:
    print(f"FAIL: pinned {missing} not on this resource. "
          f"Set PROBE_PIN from the list above.")
    sys.exit(1)

subset = [t for t in trains_in if t["train_id"] in set(PIN)]
if len(subset) < len(trains_in):
    print(f"  (restricting the model to the {len(subset)} pinned trains)")
payloads = {RESOURCE: (subset, topology)}
pins = {RESOURCE: PIN}
forced = [(FORCED, RESOURCE)]
ceiling = [(CEILING_TRAIN, RESOURCE, CEILING_S)]


def show(label, sol):
    print(f"\n--- {label}")
    print(f"    status {sol.status}   feasible {sol.feasible}"
          f"   class_costs {sol.class_costs}")
    if not sol.feasible:
        return
    for train_id in PIN:
        k = (train_id, RESOURCE)
        if k not in sol.entry_s:
            print(f"    {train_id}: absent")
            continue
        print(f"    {train_id}: entry {sol.entry_s[k]:>6}  exit {sol.exit_s[k]:>6}"
              f"  wait {sol.wait_s[k]:>6}  delay {sol.delay_s[k]:>6}"
              f"  slack {sol.slack_s[k]:>6}"
              f"  stopped {int(sol.stopped[k])}"
              f"  in_loop {int(sol.in_loop[k])}"
              f"  on_main {int(sol.on_main[k])}")


a = build_and_solve(payloads, pin_order=pins, objective=OBJECTIVE)
show("A  baseline, no extra constraints", a)

b = build_and_solve(payloads, pin_order=pins, objective=OBJECTIVE,
                    force_unstopped=forced)
show(f"B  stopped[{FORCED}] == 0", b)

c = build_and_solve(payloads, pin_order=pins, objective=OBJECTIVE,
                    force_unstopped=forced, entry_ceiling=ceiling)
show(f"C  stopped[{FORCED}] == 0  AND  entry[{CEILING_TRAIN}] <= {CEILING_S}",
     c)

print("\n=== POSITIVE CONTROL (HARNESS-NOTES day-11 figures)")
ka, kb = (CEILING_TRAIN, RESOURCE), (FORCED, RESOURCE)
checks = [
    ("A: 20172 entry == 245", a.feasible and a.entry_s.get(ka) == 245),
    ("B: 20172 entry == 344", b.feasible and b.entry_s.get(kb.__class__((CEILING_TRAIN, RESOURCE))) == 344),
    ("A: 12050 exit == 125", a.feasible and a.exit_s.get(kb) == 125),
    ("B: 12050 exit == 125", b.feasible and b.exit_s.get(kb) == 125),
    ("A: 12050 unstopped", a.feasible and not a.stopped.get(kb, True)),
    ("B: 12050 unstopped", b.feasible and not b.stopped.get(kb, True)),
]
for name, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")

print("\n=== VERDICT")
if not all(ok for _, ok in checks):
    print("  Day-11 figures NOT reproduced. The model has moved since the note")
    print("  was written, or the probe differs from it. The note's numbers no")
    print("  longer describe this code, and that is the finding. Do not read")
    print("  the C solve until this is resolved.")
elif c.feasible:
    print("  ANOMALY REAL. 245 s is feasible under the constraint and the")
    print("  solver returned 344 s as OPTIMAL. That is a solver or objective")
    print("  defect, not a modelling one.")
else:
    print("  ANOMALY DISSOLVES. 245 s is INFEASIBLE once stopped is forced to")
    print("  0, so 344 s is correct and the day-11 'strictly cheaper' claim was")
    print("  a hand-check. The biconditional at optimizer_global.py:611-612")
    print("  binds stopped to slack in both directions, so forcing stopped=0")
    print("  also bounds the schedule. The clamp repair is unblocked.")