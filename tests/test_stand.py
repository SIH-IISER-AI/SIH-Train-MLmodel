import json, sys
sys.path.insert(0, "shared"); sys.path.insert(0, "simulator"); sys.path.insert(0, "ai-engine")
from injector import LiveTelemetryInjector
from detector import ConflictDetector

network = json.load(open("data/network.json"))
scenario = json.load(open("data/scenario.json"))
fleet = {t["train_id"]: t for t in scenario["trains"]}
inj = LiveTelemetryInjector(network, scenario)
det = ConflictDetector(network, fleet)

def step(n=1):
    for _ in range(n):
        for ev in inj.tick():
            det.ingest(ev)

step(5)
before = [(c["resource_id"], sorted(c["conflicting_train_ids"])) for c in det.detect_grouped()]
print("before:", before)

inj.submit_directive({
    "kind": "STAND_ON_MAIN", "train_id": "12280", "station_id": "KSV",
    "until_train_id": "12626", "max_hold_seconds": 3000,
})
step(1)
t = inj.trains["12280"]
print("flags:", {k: v for k, v in inj._to_event(t).items()
                 if "hold" in k or "loop" in k or "standing" in k})
print("after :", [(c["resource_id"], sorted(c["conflicting_train_ids"]))
                  for c in det.detect_grouped()])

for i in range(80):
    step(5)
    t = inj.trains["12280"]
    if t.speed_kmh < 1.0 and t.standing_on_main:
        print(f"tick {inj.tick_id}: 12280 STANDING at {t.distance_km:.2f} km "
              f"(KSV is {t.station_km['KSV']:.2f})")
        break
else:
    print("FAIL: never came to a stand")

print("in_loop (must be None):", inj.trains["12280"].in_loop)
print("occupies:", inj.trains["12280"].occupied_resources(inj.topology))
print("standing conflicts:", [(c["resource_id"], sorted(c["conflicting_train_ids"]))
                              for c in det.detect_grouped()])

for i in range(200):
    step(5)
    if not inj.trains["12280"].standing_on_main:
        print(f"tick {inj.tick_id}: discharged, speed {inj.trains['12280'].speed_kmh:.1f}")
        break
else:
    print("NOTE: still standing after 200 steps -- check max_hold sizing")