"""Audit OPT-1's directives: what the simulator refuses, and what it accepts
but cannot execute.

Runs the real optimiser over a scenario's tick-0 conflicts and applies the same
acceptance rules as LiveTelemetryInjector._drain_directives. Two distinct
failure classes are reported, and the second is the more serious one:

  REFUSED -- the injector drops the directive outright. The controller can
  press Approve and watch nothing happen. Modes checked:
    * STAND_ON_MAIN whose until_train_id runs the SAME direction; an overtake
      needs a loop, and standing on the main deadlocks the train waited for
    * STAND_ON_MAIN whose station the train has already passed
    * STAND_ON_MAIN with no station to name, so no directive is emitted at all

  UNEXECUTABLE -- the injector accepts every directive and the plan still
  cannot be run. _solve_order enforces loop NoOverlap WITHIN one conflict, but
  conflicts are solved independently and Station.loop_for returns the smallest
  fitting loop, so two conflicts can book the same loop with no coordination
  between them. Nothing in _drain_directives checks loop capacity, so this
  produces zero refusals while being physically impossible. It is the case the
  global model exists to fix.

Also logs policy_exceeded, the fourth day-2 measurement: how many conflicts
needed the relaxed 3x anti-starvation cap before any ordering became feasible.
A nine-train group with hours of queueing is the strongest test this flag has
had; a zero here is a result, not a gap.

Caveat on the loop audit: directives carry release_timeout_seconds but not a
hold start time, so cross-conflict contention flagged here is POTENTIAL. Two
bookings of one loop at genuinely disjoint times would still be flagged. Treat
a flag as "inspect this", not "proved broken".

Usage:  python3 tests/count_refusals.py [data/scenario10.json]
"""
from __future__ import annotations

import json
import sys
from collections import Counter

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

from detector import ConflictDetector
from injector import LiveTelemetryInjector
from main import SOLVE_WITHIN_S, conflict_id_for
from optimizer import optimize_precedence

sys.path.insert(0, "tests")
from verify_scenario import kept_ids


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
    network = json.load(open("data/network.json"))
    scenario = json.load(open(path))
    fleet = {t["train_id"]: t for t in scenario["trains"]}

    inj = LiveTelemetryInjector(network, scenario)
    det = ConflictDetector(network, fleet)
    for event in inj.tick():
        det.ingest(event)

    direction = {tid: t.position.direction for tid, t in inj.trains.items()}
    distance = {tid: t.distance_km for tid, t in inj.trains.items()}
    station_km = {tid: t.station_km for tid, t in inj.trains.items()}

    # Same survivor rule as evaluate(): soonest contention wins, NOT the largest
    # group. A different survivor means different _windows, different entry
    # stations and different directives.
    solved: dict = {}
    for g in det.detect_grouped():
        if g["predicted_time_to_conflict_seconds"] > SOLVE_WITHIN_S:
            continue
        if not g["actionable"]:
            continue
        key = conflict_id_for(g)
        incumbent = solved.get(key)
        if incumbent is None or (
            g["predicted_time_to_conflict_seconds"]
            < incumbent["predicted_time_to_conflict_seconds"]
        ):
            solved[key] = g

    tally: Counter = Counter()
    # Seed the key so it prints even when it never fires. A Counter omits keys
    # at zero, and "absent" would be indistinguishable from "measured as zero"
    # -- which is exactly the distinction this flag is here to establish.
    tally["policy_exceeded"] = 0
    detail: list = []
    plans: dict = {}

    for key, g in solved.items():
        payload_trains, payload_topology = det.optimiser_inputs(g)
        scenarios = optimize_precedence(payload_trains, payload_topology)
        if not scenarios:
            tally["no_scenario"] += 1
            continue

        # Set when the first pass found no feasible ordering and the relaxed
        # 3x max_hold cap did. Stamped on every scenario from one solve, so
        # read it once per conflict rather than once per option.
        if scenarios[0].get("policy_exceeded"):
            tally["policy_exceeded"] += 1

        plans[key] = scenarios[0]["directives"]

        for directive in plans[key]:
            kind = directive["kind"]
            tid = directive["train_id"]
            tally[f"emitted:{kind}"] += 1

            if kind == "REGULATE":
                continue

            sid = directive.get("station_id")
            if sid is None:
                if kind == "STAND_ON_MAIN":
                    tally["refused:no_station"] += 1
                    detail.append((key, tid, kind, "no station_id"))
                else:
                    # The injector retargets to _next_loop_station; only a None
                    # from that is a refusal, and we can't evaluate it here.
                    tally["retargeted:no_station"] += 1
                continue

            km = station_km.get(tid, {}).get(sid)
            if km is None or km < distance.get(tid, 0.0) - 0.05:
                label = ("refused:passed_station" if kind == "STAND_ON_MAIN"
                         else "retargeted:passed_station")
                tally[label] += 1
                detail.append((key, tid, kind, f"{sid} already passed"))
                continue

            if kind == "STAND_ON_MAIN":
                other = directive.get("until_train_id")
                if other and direction.get(other) == direction.get(tid):
                    tally["refused:same_direction"] += 1
                    detail.append((key, tid, kind,
                                   f"same-direction {other}; overtake needs a loop"))

    emitted = sum(v for k, v in tally.items() if k.startswith("emitted:"))
    refused = sum(v for k, v in tally.items() if k.startswith("refused:"))

    print(f"scenario: {path}   conflicts solved: {len(solved)}")
    print(f"directives emitted (OPT-1): {emitted}")
    if emitted:
        print(f"refused by the simulator  : {refused}   "
              f"({100.0 * refused / emitted:.0f}%)")
    else:
        print("refused by the simulator  : n/a (nothing emitted)")
    print()
    for k in sorted(tally):
        print(f"  {k:<32}{tally[k]}")
    if detail:
        print("\ndetail")
        for key, tid, kind, why in detail:
            print(f"  {key}  {tid:>6}  {kind:<14} {why}")

    # Loop capacity is NoOverlap WITHIN a conflict, but the conflicts are solved
    # independently. Two plans can book the same loop for overlapping windows
    # and the injector accepts both -- an unexecutable plan that produces no
    # refusal. This is the case the global model exists to fix.
    loop_bookings: dict = {}
    for key in solved:
        for directive in plans.get(key, []):
            if directive["kind"] != "HOLD_AT_LOOP":
                continue
            loop_id = directive.get("loop_id")
            if loop_id:
                loop_bookings.setdefault(loop_id, []).append(
                    (key, directive["train_id"])
                )

    contested = 0
    print("\nloop bookings (cross-conflict contention is the unexecutable case)")
    if not loop_bookings:
        print("  none -- no HOLD_AT_LOOP directives in this tick")
    for loop_id, bookings in sorted(loop_bookings.items()):
        conflicts_using = {k for k, _ in bookings}
        flag = ""
        if len(conflicts_using) > 1:
            contested += 1
            flag = "  <-- CONTESTED ACROSS CONFLICTS"
        print(f"  {loop_id:<18} {len(bookings)} train(s) "
              f"from {len(conflicts_using)} conflict(s){flag}")
        for k, tid in bookings:
            print(f"      {k}  {tid}")

    # Cross-conflict coordination: the six conflicts are solved independently,
    # so one train can receive directives from several solves in the same
    # evaluate with nothing reconciling them. Loop double-booking was the
    # obvious symptom and it did not appear; disagreeing instructions are the
    # same defect wearing different clothes.
    by_train: dict = {}
    for key, directives in plans.items():
        for d in directives:
            by_train.setdefault(d["train_id"], []).append((key, d))

    print("\ncross-conflict directive collisions")
    collisions = 0
    for tid, entries in sorted(by_train.items()):
        if len(entries) < 2:
            continue
        kinds = {d["kind"] for _, d in entries}
        stations = {d.get("station_id") for _, d in entries if d.get("station_id")}
        # Three REGULATE directives at three different target speeds are as
        # contradictory as a hold and a run -- the simulator applies whichever
        # arrives last and there is no defined ordering.
        speeds = {round(float(d["target_speed_kmh"]), 1)
                  for _, d in entries if d["kind"] == "REGULATE"}
        contradictory = len(kinds) > 1 or len(stations) > 1 or len(speeds) > 1
        collisions += 1 if contradictory else 0
        flag = "  <-- CONTRADICTORY" if contradictory else ""
        print(f"  {tid}  {len(entries)} directives from "
              f"{len({k for k, _ in entries})} conflicts{flag}")
        for key, d in entries:
            speed = d.get("target_speed_kmh")
            speed_txt = f" target={float(speed):.1f}km/h" if speed is not None else ""
            print(f"      {key}  {d['kind']:<14} "
                  f"station={d.get('station_id')} until={d.get('until_train_id')}"
                  f"{speed_txt}")
    print(f"  trains with contradictory instructions: {collisions}")

    print("\ndirective coverage")
    uncovered_total = 0
    for key, g in solved.items():
        members = set(g["conflicting_train_ids"])
        kept = kept_ids(g, det)
        targeted = {d["train_id"] for d in plans.get(key, [])}
        # The lead train correctly receives no directive -- it is the one that
        # runs unimpeded, and every directive names it as until_train_id.
        # Counting it as uncovered adds one phantom per conflict.
        leads = {d.get("until_train_id") for d in plans.get(key, [])} - {None}
        untargeted = members - targeted
        if not leads and untargeted:
            # An all-REGULATE plan names no until_train_id, so fall back to the
            # highest-priority train that received nothing -- that is the one
            # the solver let run unimpeded.
            leads = {max(untargeted, key=lambda t: det.trains[t].priority)}
        uncovered = len(members - targeted - leads)
        uncovered_total += uncovered
        print(f"  {key}  group={len(members)} kept={len(kept)} "
              f"targeted={len(targeted)} "
              f"dropped-and-targeted={len(targeted & (members - kept))} "
              f"lead={len(leads & members)} "
              f"uncovered={uncovered}")

    print(f"\nsummary: refused={refused}  contested_loops={contested}  "
          f"contradictory_instructions={collisions}  "
          f"uncovered_trains={uncovered_total}  "
          f"policy_exceeded={tally['policy_exceeded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())