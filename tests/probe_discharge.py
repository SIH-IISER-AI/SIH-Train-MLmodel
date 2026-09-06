import json, os, sys, collections
sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")
sys.path.insert(0, "tests")
from detector import ConflictDetector
from main import solvable_conflicts
from optimizer_global import optimize_global
from harness import build_injector, NETWORK_PATH, SCENARIO_PATH

SEED = int(os.getenv("SEED", "3"))
TICKS = int(os.getenv("TICKS", "10800"))

network = json.load(open(NETWORK_PATH))
base = json.load(open(SCENARIO_PATH))
inj, scenario, _ = build_injector(base, network, SEED)
det = ConflictDetector(network, {t["train_id"]: t for t in scenario["trains"]})

def identity(train):
    return {
        "seq": train.hold_seq,
        "station": train.hold_station_id,
        "until": train.hold_until_train_id,
        "expires_at": train.hold_expires_sim_s,
        "in_loop": train.in_loop is not None,
        "on_main": train.standing_on_main,
    }

def geometry(train, prior):
    leader = inj.trains.get(prior["until"] or "")
    marker = None
    if leader is not None and prior["station"] is not None:
        marker = leader.station_km.get(prior["station"])
    return {
        "leader": None if leader is None else leader.train_id,
        "leader_km": None if leader is None else round(leader.distance_km, 2),
        "leader_held": None if leader is None else leader.hold_seq is not None,
        "leader_speed": None if leader is None else round(leader.speed_kmh, 1),
        "marker": None if marker is None else round(marker, 2),
    }

def classify(prior, geo):
    if prior["expires_at"] is not None and inj.elapsed_sim_seconds >= prior["expires_at"]:
        return "timeout"
    if prior["until"] is None:
        return "no_leader_named"
    if geo["leader"] is None or prior["station"] is None:
        return "leader_absent_SPURIOUS"
    if geo["marker"] is None:
        return "marker_not_on_leader_route_SPURIOUS"
    if geo["leader_km"] > geo["marker"] + 1.0:
        return "leader_passed"
    return "UNEXPLAINED"

kinds = collections.Counter()
leader_held_at_timeout = collections.Counter()
rows = []
approved = set()

for tick in range(1, TICKS + 1):
    before = {tid: identity(t) for tid, t in inj.trains.items()
              if t.hold_seq is not None}
    for event in inj.tick():
        det.ingest(event)
    for tid, prior in before.items():
        train = inj.trains[tid]
        if train.hold_seq == prior["seq"]:
            continue
        if train.hold_seq is not None:
            kinds["superseded"] += 1
            continue
        geo = geometry(train, prior)
        reason = classify(prior, geo)
        kinds[reason] += 1
        if reason == "timeout":
            leader_held_at_timeout[str(geo["leader_held"])] += 1
        rows.append((tick, tid, prior["in_loop"], reason, geo))

    candidates, _ = solvable_conflicts(det)
    if not candidates:
        continue
    plans = optimize_global(det, candidates, max_scenarios=1)
    for conflict_id, scenarios in plans.items():
        if conflict_id in approved:
            continue
        approved.add(conflict_id)
        for directive in scenarios[0].get("directives", []):
            inj.submit_directive(directive)

discharges = sum(v for k, v in kinds.items() if k != "superseded")
print(f"seed {SEED}, {TICKS} ticks")
print(f"  discharges detected   {discharges}")
print(f"  supersedes            {kinds['superseded']}")
print()
for reason, n in kinds.most_common():
    if reason == "superseded":
        continue
    pct = 100.0 * n / discharges if discharges else 0.0
    print(f"    {reason:38} {n:5d}   {pct:5.1f}%")
print()
print("at a TIMEOUT discharge, was the leader itself held?")
for value, n in leader_held_at_timeout.most_common():
    print(f"    leader_held={value:<6} {n}")
print()
print("first 25 discharges")
for tick, tid, in_loop, reason, geo in rows[:25]:
    where = "loop " if in_loop else "main "
    print(f"  t{tick:<6} {tid:<7} {where} {reason:<38} "
          f"leader={geo['leader']} km={geo['leader_km']} "
          f"marker={geo['marker']} held={geo['leader_held']}")
print()
print("POSITIVE CONTROL: discharges should be near the 62 that")
print("SIM_TRACE_HOLDS recorded on seed 3 (49 discharged_loop + 13")
print("discharged_stand). A wildly different number means this probe's")
print("approval policy diverged from the harness and nothing below is")
print("comparable to the trace.")