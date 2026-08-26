"""Day-7 gate: does the global encoding reproduce enumerate, exactly?

Method (your reframe, not the original spec's): do NOT compare which order
each model picks. Pin precedes[i,j,r] to enumerate's OPT-1 order and compare
the resulting entry times. That isolates the ENCODING -- interval variables,
NoOverlap, loop capacity, and the constants from _prepare -- from an objective
that does not exist until day 10. With the order pinned the global model has
no freedom to coordinate differently, so any mismatch is unambiguously an
encoding error.

Tolerance is EXACT. Both models receive the same integer-second constants from
the same _prepare call. If the encoding is right the times are equal, not
close. A tolerance you cannot name the rounding step for is a tolerance that
hides the bug.

Two modes, and running both is what makes a failure interpretable:

  isolated  each conflict in its own model. Reproduces enumerate's scoping
            exactly. This must pass -- it is the encoding test.
  joint     both conflicts in ONE model. May legitimately differ if the two
            conflicts share a loop or a resource, because that coupling is
            the thing per-conflict solving cannot see. A joint-only failure is
            a finding, not a bug, and the shared-object report says which.

Usage:  python3 tests/test_global_encoding.py [data/scenario.json]
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

from detector import ConflictDetector
from injector import LiveTelemetryInjector
from optimizer import DEFAULT_MAX_HOLD_SECONDS, optimize_precedence
from optimizer_global import build_and_solve, chain_links

failures = []


def check(what, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got} (want {want})")
    if not ok:
        failures.append(what)


def compare(label, resource_id, expected_per_train, solution):
    """Every solved field, per train, exact."""
    for train_id, want in sorted(expected_per_train.items()):
        key = (train_id, resource_id)
        if key not in solution.entry_s:
            check(f"{label} {train_id} present", False, True)
            continue
        for field, got_map in (
            ("entry_s", solution.entry_s), ("exit_s", solution.exit_s),
            ("wait_s", solution.wait_s), ("delay_s", solution.delay_s),
        ):
            check(f"{label} {train_id}.{field}", got_map[key], want[field])
        check(f"{label} {train_id}.stopped", solution.stopped[key], want["stopped"])
        check(f"{label} {train_id}.in_loop", solution.in_loop[key], want["in_loop"])
        check(f"{label} {train_id}.on_main", solution.on_main[key], want["on_main"])


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario.json"
    network = json.load(open("data/network.json"))
    scenario = json.load(open(path))
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
        return 0

    print(f"scenario: {path}   first conflict at tick {tick}   "
          f"conflicts: {len(conflicts)}\n")

    payloads, pins, expected, trains_on = {}, {}, {}, {}
    for conflict in conflicts:
        resource_id = conflict["resource_id"]
        if resource_id in payloads:
            continue
        trains_in, topology = det.optimiser_inputs(conflict)
        scenarios = optimize_precedence(trains_in, topology)
        if not scenarios:
            print(f"  {resource_id}: enumerate returned nothing -- skipped")
            continue
        best = scenarios[0]

        # If enumerate needed the 3x relaxation, it solved a DIFFERENT topology
        # than the one passed in. Handing the global model the unrelaxed one
        # would fail the gate on a constant, not on the encoding.
        if best.get("policy_exceeded"):
            topology = dict(topology)
            topology["max_hold_seconds"] = int(
                topology.get("max_hold_seconds", DEFAULT_MAX_HOLD_SECONDS) * 3
            )
            print(f"  {resource_id}: policy_exceeded -- using the relaxed cap")

        # optimize_precedence sorts by priority and truncates to
        # MAX_TRAINS_ENUMERATED, so on a large group enumerate's order covers
        # fewer trains than optimiser_inputs handed over. Give the global model
        # the SAME set: the extra trains contend the same resource and would
        # shift the others, failing the gate for a reason that is not an
        # encoding bug. Carrying trains enumerate discards is the global
        # model's advantage, and it gets measured on day 12 -- not here, where
        # the only question is whether the encoding is faithful.
        enumerated = set(best["order_train_ids"])
        payloads[resource_id] = (
            [t for t in trains_in if t["train_id"] in enumerated], topology
        )
        pins[resource_id] = best["order_train_ids"]
        if len(enumerated) < len(trains_in):
            print(f"    (cap dropped {len(trains_in) - len(enumerated)} of "
                  f"{len(trains_in)}; comparing the enumerated subset)")
        expected[resource_id] = best["per_train"]
        trains_on[resource_id] = {t["train_id"] for t in trains_in}
        print(f"  {resource_id}: {len(trains_in)} trains, "
              f"OPT-1 order {' -> '.join(best['order_train_ids'])}")

    if not payloads:
        print("SKIP: nothing to compare")
        return 0

    print("\n--- isolated (one model per conflict) ---")
    for resource_id, payload in payloads.items():
        solution = build_and_solve(
            {resource_id: payload}, pin_order={resource_id: pins[resource_id]}
        )
        check(f"{resource_id} solved", solution.status, "OPTIMAL")
        if solution.feasible:
            compare("iso", resource_id, expected[resource_id], solution)
            check(f"{resource_id} precedes count",
                  solution.counts["precedes"],
                  len(pins[resource_id]) * (len(pins[resource_id]) - 1))

    if len(payloads) > 1:
        print("\n--- joint (all conflicts in one model) ---")
        # Pairwise, not an intersection across all resources -- with 15
        # conflicts a global intersection is empty by construction and the
        # diagnostic silently reports "none" exactly when it matters most.
        from collections import Counter
        train_hits = Counter(t for s in trains_on.values() for t in s)
        shared_trains = {t for t, n in train_hits.items() if n > 1}
        loops = {r: {t.get("hold_loop_id") for t in p[0] if t.get("hold_loop_id")}
                 for r, p in payloads.items()}
        loop_hits = Counter(l for s in loops.values() for l in s)
        shared_loops = {l for l, n in loop_hits.items() if n > 1}
        print(f"  shared resources: none by construction (keys are distinct)")
        print(f"  shared trains   : {sorted(shared_trains) or 'none'}")
        print(f"  shared loops    : {sorted(shared_loops) or 'none'}")

        joint = build_and_solve(payloads, pin_order=pins)
        check("joint solved", joint.status, "OPTIMAL")
        if joint.feasible:
            # The pre-chaining exemption is GONE, and so is the exact
            # comparison it sat inside. It has to go: before day 8 the joint
            # model was the UNION of independent models, so an entry-time
            # mismatch was an encoding error. After chaining it is not. A train
            # held at resource k arrives at k+1 later BY DESIGN, and that
            # displacement propagates to whatever follows it at k+1. Comparing
            # entry_s against a per-conflict engine that cannot see the hold
            # would fail the gate for the exact behaviour the gate exists to
            # produce.
            #
            # So: the ENCODING verdict is the isolated run above, unchanged and
            # exact. The joint run is now a STRUCTURAL gate plus a coordination
            # report. Every assertion below is a property the model must have
            # regardless of what enumerate would have said.
            print("\n  displacement vs enumerate (coordination, not failure):")
            for resource_id in sorted(payloads):
                for train_id, want in sorted(expected[resource_id].items()):
                    key = (train_id, resource_id)
                    if key not in joint.entry_s:
                        continue
                    shift = joint.entry_s[key] - want["entry_s"]
                    if shift:
                        print(f"    {train_id:>6} on {resource_id:<26} "
                              f"entry {shift:+6}s  slack "
                              f"{joint.slack_s[key]:>5}s  delay "
                              f"{joint.delay_s[key]:>5}s (enumerate said "
                              f"{want['delay_s']}s)")

            # ---- day-8/9 structural assertions ------------------------------
            print("\n--- chaining (structural, not a comparison) ---")
            prepared = {r: joint.prepared[r] for r in payloads}
            links = chain_links(prepared)
            check("chain links built", len(links) > 0, True)
            for link in links:
                here = (link.train_id, link.from_resource)
                there = (link.train_id, link.to_resource)
                if here not in joint.entry_s or there not in joint.entry_s:
                    continue
                want = (
                    joint.entry_s[here]
                    + link.travel_s
                    + (link.stop_extra_s if joint.stopped[here] else 0)
                )
                check(
                    f"chain {link.train_id} "
                    f"{link.from_resource}->{link.to_resource}",
                    joint.ready_s[there], want,
                )
                check(
                    f"no negative slack {link.train_id} {link.to_resource}",
                    joint.slack_s[there] >= 0, True,
                )

            # LOOP double-booking. One train, several adjacent blocks, one
            # loop: it stands ONCE. More than one in_loop on the same loop_id
            # for the same train means chaining did not collapse the stand.
            print("\n--- loop identity (the withdrawn LOOP-MTJ-01 claim) ---")
            booked = {}
            for (train_id, resource_id), berthed in joint.in_loop.items():
                if not berthed:
                    continue
                train = joint.train_of((train_id, resource_id))
                booked.setdefault((train_id, train.loop_id), []).append(resource_id)
            for (train_id, loop_id), resources in sorted(booked.items()):
                check(f"{train_id} berths in {loop_id} once",
                      len(resources), 1)
            if not booked:
                print("  (no loop berths in this solution)")

            # One stand per train. This CANNOT be asserted against the pinned
            # solve: the pin reproduces enumerate's per-conflict orders, and on
            # scenario.json those orders force 40201 to stand twice -- once at
            # BLK-115D behind 12626, once at SEC-PWL-KSV behind 12280. Both
            # slacks exceed absorbable, so both stops are forced, and
            # sum(stopped) <= 1 under that pin is INFEASIBLE rather than
            # one-stop. That is why the constraint carries `not pin_order`.
            #
            # The finding is the point. The simulator holds ONE hold flag per
            # train (standing_on_main / in_loop_id / hold_station_id are
            # scalars), so a two-stop schedule cannot be emitted as directives
            # at all. Enumerate's composed plan is not executable -- an
            # argument that needs no delay statistic.
            print("\n--- stands under the pinned order (enumerate's plan) ---")
            for train_id in sorted({t for t, _ in joint.entry_s}):
                stands = sorted(
                    r for (t, r), flag in joint.stopped.items()
                    if t == train_id and flag
                )
                if len(stands) > 1:
                    print(f"    FINDING {train_id} must stand {len(stands)}x: "
                          f"{stands} -- not expressible as directives")

            # The assertion belongs on the plan that actually ships: unpinned,
            # where the model chooses precedence and GLOBAL_MAX_STOPS binds.
            print("\n--- one stand per train (unpinned: the shipped plan) ---")
            free = build_and_solve(payloads)
            check("unpinned joint solved", free.status, "OPTIMAL")
            if free.feasible:
                for train_id in sorted({t for t, _ in free.entry_s}):
                    stands = sorted(
                        r for (t, r), flag in free.stopped.items()
                        if t == train_id and flag
                    )
                    if len(stands) > 1:
                        print(f"    {train_id} stands at {stands}")
                    check(f"{train_id} is brought to a stand at most once",
                          len(stands) <= 1, True)

            # Issue 3: no double counting. A train's cumulative hold is the sum
            # of the stands imposed on it, never the sum of its lateness.
            print("\n--- cumulative hold (decision 4) ---")
            for train_id in sorted({t for t, _ in joint.entry_s}):
                keys = [k for k in joint.slack_s if k[0] == train_id]
                check(f"total_hold[{train_id}] is the sum of its slacks",
                      joint.total_hold_s.get(train_id),
                      sum(joint.slack_s[k] for k in keys))

    print("\nFAIL: " + "; ".join(failures[:12]) if failures else "\nPASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())