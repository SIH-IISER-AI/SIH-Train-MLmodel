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

The measurement itself now lives in audit_plans(), which takes already-solved
scenarios rather than solving them. tests/harness.py calls it once per
approval; this script calls it once at tick 0 and prints. One implementation,
so a day-4 CSV column and a day-2 report line cannot mean two different things.

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
from main import solvable_conflicts
from optimizer import optimize_precedence

sys.path.insert(0, "tests")
from verify_scenario import kept_ids


def audit_plans(solved, scenarios_by_conflict, det, inj):
    """Score one evaluate's worth of OPT-1 plans against the simulator's rules.

        solved                  conflict_id -> group, from solvable_conflicts()
        scenarios_by_conflict   conflict_id -> what optimize_precedence returned
        det                     ConflictDetector, for priority and membership
        inj                     LiveTelemetryInjector, for live direction/position

    Returns a dict whose "summary" holds the scalars that reach a CSV or a
    report, plus the raw structures the printer below needs. Nothing is solved
    and nothing is printed here, so a caller running this once per tick pays
    only for the bookkeeping.
    """
    direction = {tid: t.position.direction for tid, t in inj.trains.items()}
    distance = {tid: t.distance_km for tid, t in inj.trains.items()}
    station_km = {tid: t.station_km for tid, t in inj.trains.items()}

    tally: Counter = Counter()
    # Seed the key so it prints even when it never fires. A Counter omits keys
    # at zero, and "absent" would be indistinguishable from "measured as zero"
    # -- which is exactly the distinction this flag is here to establish.
    tally["policy_exceeded"] = 0
    detail: list = []
    plans: dict = {}

    for key in solved:
        scenarios = scenarios_by_conflict.get(key) or []
        if not scenarios:
            tally["no_scenario"] += 1
            continue

        # Set when the first pass found no feasible ordering and the relaxed
        # 3x max_hold cap did. Stamped on every scenario from one solve, so
        # read it once per conflict rather than once per option.
        if scenarios[0].get("policy_exceeded"):
            tally["policy_exceeded"] += 1

        plans[key] = scenarios[0].get("directives", [])

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

    contested = sum(
        1 for bookings in loop_bookings.values()
        if len({k for k, _ in bookings}) > 1
    )

    # Cross-conflict coordination: the conflicts are solved independently, so
    # one train can receive directives from several solves in the same evaluate
    # with nothing reconciling them. Loop double-booking was the obvious
    # symptom and it did not appear; disagreeing instructions are the same
    # defect wearing different clothes.
    by_train: dict = {}
    for key, directives in plans.items():
        for d in directives:
            by_train.setdefault(d["train_id"], []).append((key, d))

    collisions = 0
    collision_rows: list = []
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
        collision_rows.append((tid, entries, contradictory))

    coverage: list = []
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
            # the solver let run unimpeded. sorted() first because max() returns
            # the first maximal element in ITERATION order and the two freights
            # tie at w=2.0: over a raw set that makes the answer depend on
            # PYTHONHASHSEED, i.e. on the process rather than on the scenario.
            leads = {max(sorted(untargeted),
                         key=lambda t: det.trains[t].priority)}
        uncovered = len(members - targeted - leads)
        uncovered_total += uncovered
        coverage.append({
            "conflict_id": key,
            "group": len(members),
            "kept": len(kept),
            "targeted": len(targeted),
            "dropped_and_targeted": len(targeted & (members - kept)),
            "lead": len(leads & members),
            "uncovered": uncovered,
        })

    emitted = sum(v for k, v in tally.items() if k.startswith("emitted:"))
    refused = sum(v for k, v in tally.items() if k.startswith("refused:"))

    return {
        "summary": {
            "conflicts": len(solved),
            "emitted": emitted,
            "refused": refused,
            "contested_loops": contested,
            "contradictory_instructions": collisions,
            "uncovered_trains": uncovered_total,
            "policy_exceeded": tally["policy_exceeded"],
            "no_scenario": tally["no_scenario"],
        },
        "tally": tally,
        "detail": detail,
        "plans": plans,
        "loop_bookings": loop_bookings,
        "collision_rows": collision_rows,
        "coverage": coverage,
    }


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
    network = json.load(open("data/network.json"))
    scenario = json.load(open(path))
    fleet = {t["train_id"]: t for t in scenario["trains"]}

    inj = LiveTelemetryInjector(network, scenario)
    det = ConflictDetector(network, fleet)
    for event in inj.tick():
        det.ingest(event)

    # Same filter chain and survivor rule as evaluate(), imported rather than
    # restated: soonest contention wins, NOT the largest group.
    solved, _counts = solvable_conflicts(det)

    scenarios_by_conflict = {}
    for key, group in solved.items():
        payload_trains, payload_topology = det.optimiser_inputs(group)
        scenarios_by_conflict[key] = optimize_precedence(
            payload_trains, payload_topology
        )

    audit = audit_plans(solved, scenarios_by_conflict, det, inj)
    summary = audit["summary"]
    tally = audit["tally"]

    print(f"scenario: {path}   conflicts solved: {summary['conflicts']}")
    print(f"directives emitted (OPT-1): {summary['emitted']}")
    if summary["emitted"]:
        print(f"refused by the simulator  : {summary['refused']}   "
              f"({100.0 * summary['refused'] / summary['emitted']:.0f}%)")
    else:
        print("refused by the simulator  : n/a (nothing emitted)")
    print()
    for k in sorted(tally):
        print(f"  {k:<32}{tally[k]}")
    if audit["detail"]:
        print("\ndetail")
        for key, tid, kind, why in audit["detail"]:
            print(f"  {key}  {tid:>6}  {kind:<14} {why}")

    print("\nloop bookings (cross-conflict contention is the unexecutable case)")
    if not audit["loop_bookings"]:
        print("  none -- no HOLD_AT_LOOP directives in this tick")
    for loop_id, bookings in sorted(audit["loop_bookings"].items()):
        conflicts_using = {k for k, _ in bookings}
        flag = "  <-- CONTESTED ACROSS CONFLICTS" if len(conflicts_using) > 1 else ""
        print(f"  {loop_id:<18} {len(bookings)} train(s) "
              f"from {len(conflicts_using)} conflict(s){flag}")
        for k, tid in bookings:
            print(f"      {k}  {tid}")

    print("\ncross-conflict directive collisions")
    for tid, entries, contradictory in audit["collision_rows"]:
        flag = "  <-- CONTRADICTORY" if contradictory else ""
        print(f"  {tid}  {len(entries)} directives from "
              f"{len({k for k, _ in entries})} conflicts{flag}")
        for key, d in entries:
            speed = d.get("target_speed_kmh")
            speed_txt = f" target={float(speed):.1f}km/h" if speed is not None else ""
            print(f"      {key}  {d['kind']:<14} "
                  f"station={d.get('station_id')} until={d.get('until_train_id')}"
                  f"{speed_txt}")
    print(f"  trains with contradictory instructions: "
          f"{summary['contradictory_instructions']}")

    print("\ndirective coverage")
    for row in audit["coverage"]:
        print(f"  {row['conflict_id']}  group={row['group']} kept={row['kept']} "
              f"targeted={row['targeted']} "
              f"dropped-and-targeted={row['dropped_and_targeted']} "
              f"lead={row['lead']} uncovered={row['uncovered']}")

    print(f"\nsummary: refused={summary['refused']}  "
          f"contested_loops={summary['contested_loops']}  "
          f"contradictory_instructions={summary['contradictory_instructions']}  "
          f"uncovered_trains={summary['uncovered_trains']}  "
          f"policy_exceeded={summary['policy_exceeded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())