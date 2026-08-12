import importlib, json, os, sys

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

import detector as detector_module
from optimizer import optimize_precedence
from injector import LiveTelemetryInjector

failures = []


def check(what, got, want):
    print(f"{'ok  ' if got == want else 'FAIL'} {what}: {got} (want {want})")
    if got != want:
        failures.append(what)


os.environ.pop("MAX_HOLD_SECONDS", None)
os.environ.pop("MAX_REGULATION_SECONDS", None)
detector_module = importlib.reload(detector_module)
check("default cap", detector_module.MAX_HOLD_SECONDS, 900)
check("default regulation ceiling", detector_module.MAX_REGULATION_SECONDS, 300)

os.environ["MAX_HOLD_SECONDS"] = "600"
os.environ["MAX_REGULATION_SECONDS"] = "240"
detector_module = importlib.reload(detector_module)
check("env cap", detector_module.MAX_HOLD_SECONDS, 600)

network = json.load(open("data/network.json"))
scenario = json.load(open("data/scenario.json"))
fleet = {t["train_id"]: t for t in scenario["trains"]}
inj = LiveTelemetryInjector(network, scenario)
det = detector_module.ConflictDetector(network, fleet)

conflicts = []
for _ in range(120):
    for ev in inj.tick():
        det.ingest(ev)
    conflicts = det.detect_grouped()
    if conflicts:
        break

if not conflicts:
    print("SKIP: no conflict on this seed within 120 ticks")
else:
    trains_in, topo = det.optimiser_inputs(conflicts[0])
    check("cap reaches the solver", topo["max_hold_seconds"], 600)
    check("regulation ceiling reaches the solver", topo["max_regulation_seconds"], 240)

    scenarios = optimize_precedence(trains_in, topo)
    print(f"\nresource {topo['resource_id']}  trains {[t['train_id'] for t in trains_in]}")
    print(f"scenarios offered: {len(scenarios)}  policy_exceeded="
          f"{scenarios[0]['policy_exceeded'] if scenarios else 'n/a'}")
    for s in scenarios:
        print(f"  {s['scenario_id']}: {s['action']}")
        print(f"      {s['rationale']}")
    for d in (scenarios[0]["directives"] if scenarios else []):
        if d["kind"] in ("HOLD_AT_LOOP", "STAND_ON_MAIN"):
            check(f"{d['kind']} carries a release timeout",
                  "release_timeout_seconds" in d, True)

print("\nFAIL: " + "; ".join(failures) if failures else "\nPASS")