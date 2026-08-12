import json, sys
sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")
from injector import LiveTelemetryInjector
from detector import ConflictDetector

network = json.load(open("data/network.json"))
scenario = json.load(open("data/scenario.json"))
fleet = {t["train_id"]: t for t in scenario["trains"]}
inj = LiveTelemetryInjector(network, scenario)
det = ConflictDetector(network, fleet)

for _ in range(30):
    for ev in inj.tick():
        det.ingest(ev)

failures = []
for t in inj.trains.values():
    e = inj._to_event(t)
    ceiling = min(t.scheduled_speed_kmh, e["max_allowed_speed_kmh"])
    print(f"{t.train_id:6} {t.train_name:20} spd={e['speed_kmh']:6.1f} "
          f"book={e['scheduled_speed_kmh']:6.1f} permitted={e['max_allowed_speed_kmh']:6.1f} "
          f"delay={e['delay_seconds']:5}s")
    if e["speed_kmh"] > ceiling + 0.5:
        failures.append(f"{t.train_id} exceeds booked ceiling")

conflicts = det.detect_grouped()
if conflicts:
    trains_in, _ = det.optimiser_inputs(conflicts[0])
    print("\noptimiser payload:")
    for x in trains_in:
        print(f"  {x['train_id']:6} target={x['target_speed_kmh']:6.1f} max={x['max_speed_kmh']:6.1f}")
        if x["target_speed_kmh"] >= x["max_speed_kmh"]:
            failures.append(f"{x['train_id']} target not below max")

print("\nFAIL: " + "; ".join(failures) if failures else "\nPASS")