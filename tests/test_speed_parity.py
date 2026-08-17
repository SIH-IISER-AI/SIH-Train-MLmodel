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

# --- F4 guard: the projection floor must not reach the solver ---------------
# `TrackedTrain.speed_kmh` is floored at MIN_PROJECTION_SPEED_KMH so a crawling
# train does not project an eight-hour occupancy. The optimiser models
# acceleration from rest explicitly, so it must see a stand as a stand: a
# floored 5 km/h buys a restart penalty the train cannot owe and an absorbable
# delay it cannot absorb.
speed_failures = []
if conflicts:
    trains_in, _ = det.optimiser_inputs(conflicts[0])
    for x in trains_in:
        raw = float(det.trains[x["train_id"]].telemetry["speed_kmh"])
        if abs(x["current_speed"] - raw) > 1e-9:
            speed_failures.append(
                f"{x['train_id']} current_speed={x['current_speed']} != raw {raw}"
            )

    victim = conflicts[0]["conflicting_train_ids"][0]
    saved = det.trains[victim].telemetry["speed_kmh"]
    det.trains[victim].telemetry["speed_kmh"] = 0.0
    forced, _ = det.optimiser_inputs(conflicts[0])
    at_rest = next(x for x in forced if x["train_id"] == victim)
    print(f"\nstand test: {victim} raw=0.0 -> payload {at_rest['current_speed']}")
    if at_rest["current_speed"] != 0.0:
        speed_failures.append(
            f"{victim} at a stand reached the solver at "
            f"{at_rest['current_speed']} km/h"
        )
    det.trains[victim].telemetry["speed_kmh"] = saved

print("SPEED-FAIL: " + "; ".join(speed_failures) if speed_failures else "SPEED-PASS")