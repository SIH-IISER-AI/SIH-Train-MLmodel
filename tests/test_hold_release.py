import json, sys

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

from detector import ConflictDetector
from injector import LiveTelemetryInjector
from optimizer import optimize_precedence

WARMUP_TICKS = 200
WATCH_TICKS = 1500

network = json.load(open("data/network.json"))
scenario = json.load(open("data/scenario.json"))
fleet = {t["train_id"]: t for t in scenario["trains"]}

inj = LiveTelemetryInjector(network, scenario)
det = ConflictDetector(network, fleet)

conflicts = []
for _ in range(WARMUP_TICKS):
    for event in inj.tick():
        det.ingest(event)
    conflicts = det.detect_grouped()
    if conflicts:
        break

if not conflicts:
    print("SKIP: no conflict produced within warmup")
    raise SystemExit(0)

trains_in, topo = det.optimiser_inputs(conflicts[0])
plan = optimize_precedence(trains_in, topo)[0]
print(f"plan: {plan['action']}\n")

holds = [d for d in plan["directives"] if d["kind"] in ("HOLD_AT_LOOP", "STAND_ON_MAIN")]
for directive in holds:
    inj.submit_directive(directive)

watched = {
    d["train_id"]: {
        "until": d.get("until_train_id"),
        "station": d.get("station_id"),
        "timeout_s": d.get("release_timeout_seconds"),
        "stopped_s": None,
        "released_s": None,
    }
    for d in holds
}

for _ in range(WATCH_TICKS):
    for event in inj.tick():
        det.ingest(event)
    for train_id, rec in watched.items():
        train = inj.trains[train_id]
        held = train.in_loop is not None or train.standing_on_main
        if rec["stopped_s"] is None:
            if held and train.speed_kmh < 1.0:
                rec["stopped_s"] = inj.elapsed_sim_seconds
        elif rec["released_s"] is None:
            if not held and train.speed_kmh > 1.0:
                rec["released_s"] = inj.elapsed_sim_seconds
    if all(r["released_s"] is not None for r in watched.values()):
        break

failures = []
print(f"watched {inj.elapsed_sim_seconds/60:.0f} sim minutes after the plan\n")

for train_id, rec in watched.items():
    train = inj.trains[train_id]
    leader = inj.trains.get(rec["until"] or "")
    marker = leader.station_km.get(rec["station"]) if leader and rec["station"] else None

    stopped = f"{rec['stopped_s']/60:.1f} min" if rec["stopped_s"] else "NEVER"
    released = f"{rec['released_s']/60:.1f} min" if rec["released_s"] else "NEVER"
    verdict = "ok  " if rec["stopped_s"] and rec["released_s"] else "FAIL"
    if verdict == "FAIL":
        failures.append(train_id)

    print(f"{verdict} {train_id}  held at {rec['station']} until {rec['until']}")
    print(f"       stopped {stopped}   released {released}")
    print(f"       in_loop={train.in_loop} standing_on_main={train.standing_on_main} "
          f"hold_station={train.hold_station_id} speed={train.speed_kmh:.0f}")
    print(f"       km={train.distance_km:.2f} authority={train.authority_km:.2f} "
          f"timeout_at={rec['timeout_s']}s elapsed={inj.elapsed_sim_seconds:.0f}s")
    if leader is not None and marker is not None:
        gap = leader.distance_km - (marker + 1.0)
        print(f"       leader {rec['until']} at {leader.distance_km:.2f} km, "
              f"marker {marker + 1.0:.2f} km, {'PASSED' if gap > 0 else f'{-gap:.2f} km short'}")
    print()

print("FAIL: " + ", ".join(failures) if failures else "PASS")
