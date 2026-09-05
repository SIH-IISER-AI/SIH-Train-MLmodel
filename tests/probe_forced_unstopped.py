import json, os, sys
sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")
from detector import ConflictDetector
from injector import LiveTelemetryInjector
from optimizer_global import HOLD_MIN_APPROACH_M, build_and_solve

SCENARIO = os.getenv("SCENARIO_PATH", "data/scenario10.json")
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

payloads = {}
for conflict in conflicts:
    resource_id = conflict["resource_id"]
    trains_in, topology = det.optimiser_inputs(conflict)
    if len(trains_in) < 2 or resource_id in payloads:
        continue
    payloads[resource_id] = (trains_in, topology)
print(f"tick {tick}: {len(payloads)} resources, "
      f"{sum(len(p[0]) for p in payloads.values())} (train, resource) keys")

def descent(label, sol):
    print(f"\n{label}: {sol.status} feasible={sol.feasible} "
          f"class_costs={sol.class_costs}")
    print(f"    tiers_completed={sol.counts.get('tiers_completed')} "
          f"truncated={sol.counts.get('truncated')} "
          f"solve_count={sol.solve_count}")
    print(f"    tier_log={sol.counts.get('tier_log')}")

base = build_and_solve(payloads, objective="lexicographic")
descent("baseline", base)
if not base.feasible:
    sys.exit(1)

forced = sorted(
    k for k in base.entry_s
    if base.train_of(k).distance_m <= HOLD_MIN_APPROACH_M
)
print(f"keys with distance_m <= {HOLD_MIN_APPROACH_M}: {len(forced)}")
for k in forced:
    print(f"    {k[0]:<7} {k[1]:<24} distance_m={base.train_of(k).distance_m:.1f}"
          f"  stopped={int(base.stopped[k])}  slack={base.slack_s[k]}")
if not forced:
    print("\nNo unreachable keys at this tick. Run another tick or scenario.")
    sys.exit(0)

test = build_and_solve(payloads, objective="lexicographic",
                       force_unstopped=forced)
descent("forced  ", test)
if not test.feasible:
    print("\nINFEASIBLE under the constraint. The repair is not free.")
    sys.exit(1)

moved = []
for k in sorted(base.entry_s):
    if k not in test.entry_s:
        moved.append((k, "absent", "", ""))
        continue
    if (base.entry_s[k] != test.entry_s[k]
            or base.delay_s[k] != test.delay_s[k]
            or base.stopped[k] != test.stopped[k]):
        moved.append((k, base.entry_s[k], test.entry_s[k],
                      f"delay {base.delay_s[k]}->{test.delay_s[k]}"
                      f"  stopped {int(base.stopped[k])}->{int(test.stopped[k])}"))

collateral = [m for m in moved if m[0] not in set(forced)]
print(f"\nkeys whose schedule moved:            {len(moved)} of {len(base.entry_s)}")
print(f"  of which NOT forced (collateral):   {len(collateral)}")
for k, a, b, extra in moved[:30]:
    mark = "forced " if k in set(forced) else "COLLAT "
    print(f"  {mark}{k[0]:<7} {k[1]:<24} entry {a} -> {b}   {extra}")

new_stops = [k for k in sorted(test.stopped)
             if test.stopped[k] and not base.stopped.get(k, False)]
print(f"\nnewly stopped keys: {len(new_stops)}")
for k in new_stops:
    t = test.train_of(k)
    print(f"    {k[0]:<7} {k[1]:<24} distance_m={t.distance_m:.1f}"
          f"  stand_station={getattr(t, 'stand_station', None)}"
          f"  loop_available={getattr(t, 'loop_available', None)}"
          f"  slack={test.slack_s[k]}"
          f"  in_loop={int(test.in_loop[k])} on_main={int(test.on_main[k])}")

bd = sum(base.delay_s.values())
td = sum(test.delay_s.values())
print(f"\ntotal delay   {bd} -> {td}   ({td - bd:+d} s)")
print(f"stops         {sum(base.stopped.values())} -> {sum(test.stopped.values())}")
print("\n=== VERDICT")
if not collateral and td <= bd:
    print("  Repair is free at this tick: only forced keys moved and total")
    print("  delay did not rise. The day-11 objection does not reproduce in")
    print("  the shipping objective.")
elif collateral:
    print(f"  {len(collateral)} keys moved that were NOT forced. This is the")
    print("  perturbation HARNESS-NOTES describes, now measured in")
    print("  lexicographic mode. Report the magnitude, not just the fact.")
else:
    print(f"  No collateral movement, but total delay rose by {td - bd} s.")
    print("  That is the price of the repair, not a defect.")