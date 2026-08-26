"""Day-9 gate: the constraint enumerate cannot state, in one file.

The defect, measured: the anti-starvation cap is scoped PER SOLVE. cap_s[i] =
forced_s[i] + max_hold_s, and forced_s is the queueing this order imposes, so
every individual approval is compliant by construction. 40201 accumulated
8,533 s across ~35 of them with policy_exceeded reading 0 throughout.

This test constructs the smallest case with that shape: one freight held behind
a premier service on TWO resources. Each hold is individually compliant. Their
sum is not.

  against optimizer.py        both conflicts solve, policy_exceeded is False on
                              both, and neither solve can see the other
  against optimizer_global.py one model, total_hold[t] = SUM over k of
                              slack[t,k], and a 900 s ceiling refuses it

That difference is the day-14 merge-gate row: max_cumulative_hold_s is a column
where the global engine should win outright rather than trade.

Usage:  python3 tests/test_global_hold.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "shared")
sys.path.insert(0, "ai-engine")

from optimizer import optimize_precedence
from optimizer_global import build_and_solve

#: Two long single-line sections. Long enough that the premier's occupancy
#: alone exceeds the 900 s discretionary ceiling, which is the whole point:
#: under the per-conflict rule that occupancy is `forced` and therefore free.
SECTIONS = ("SEC-AAA-BBB", "SEC-BBB-CCC")
SECTION_LENGTH_M = 39000.0
MAX_HOLD_S = 900

failures = []


def check(what, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got} (want {want})")
    if not ok:
        failures.append(what)


def topology(resource_id: str):
    return {
        "block_id": resource_id,
        "resource_id": resource_id,
        "junction_id": "AAA",
        "single_line": True,
        "length_m": SECTION_LENGTH_M,
        "line_speed_kmh": 100.0,
        "headway_seconds": 120,
        "max_regulation_seconds": 300,
        "max_hold_seconds": MAX_HOLD_S,
    }


def freight(projected_entry_s: int, distance_m: float):
    return {
        "train_id": "40208", "train_name": "BOXN Rake 408",
        "train_type": "FREIGHT", "current_speed": 45.0,
        "target_speed_kmh": 60.0, "max_speed_kmh": 60.0,
        "distance_to_bottleneck": distance_m,
        "projected_entry_s": float(projected_entry_s),
        "priority_weight": 2.0, "train_length_m": 700.0,
        "existing_delay_seconds": 0, "hold_station_id": "AAA",
        "hold_loop_id": "LOOP-AAA-01", "hold_loop_length_m": 800.0,
        "direction": "UP",
    }


def premier(train_id: str, name: str, projected_entry_s: int, distance_m: float):
    return {
        "train_id": train_id, "train_name": name,
        "train_type": "SHATABDI", "current_speed": 110.0,
        "target_speed_kmh": 130.0, "max_speed_kmh": 130.0,
        "distance_to_bottleneck": distance_m,
        "projected_entry_s": float(projected_entry_s),
        "priority_weight": 9.0, "train_length_m": 500.0,
        "existing_delay_seconds": 0, "hold_station_id": "AAA",
        "hold_loop_id": None, "hold_loop_length_m": 0.0,
        "direction": "DOWN",
    }


def main() -> int:
    # The freight reaches each section just after a premier service has been
    # let into it. Premier-first is both the IR precedence rule and the natural
    # arrival order, so this is not a contrived ordering -- it is the ordinary
    # one, which is what makes the accumulation invisible per conflict.
    payloads = {
        SECTIONS[0]: (
            [freight(120, 2000.0), premier("12002", "Bhopal Shatabdi", 60, 2200.0)],
            topology(SECTIONS[0]),
        ),
        # The second premier is timed to arrive AFTER the freight's chained
        # release from the first section. Without that the freight reaches
        # BBB-CCC so late that the section is already clear, slack[k+1] is 0,
        # and the composition the test is named for never happens -- it still
        # passes, but on one hop rather than two, which is a weaker slide.
        SECTIONS[1]: (
            [freight(1900, 41000.0), premier("12001", "Bhopal Shatabdi Up", 3000, 62000.0)],
            topology(SECTIONS[1]),
        ),
    }

    print("--- against optimizer.py: each conflict on its own ---")
    per_conflict_total = 0
    for resource_id, (trains_in, track) in payloads.items():
        scenarios = optimize_precedence(trains_in, track)
        if not scenarios:
            check(f"{resource_id} solved", False, True)
            continue
        best = scenarios[0]
        result = best["per_train"]["40208"]
        discretionary = int(result["discretionary_s"])
        per_conflict_total += int(result["delay_s"])
        print(f"  {resource_id}: 40208 delay {result['delay_s']}s "
              f"(forced {result['forced_s']}s, discretionary {discretionary}s)")
        check(f"{resource_id} discretionary within the {MAX_HOLD_S}s cap",
              discretionary <= MAX_HOLD_S, True)
        check(f"{resource_id} policy_exceeded", best["policy_exceeded"], False)

    print(f"\n  two compliant approvals, {per_conflict_total}s on one train")
    check("the sum is over the ceiling", per_conflict_total > MAX_HOLD_S, True)

    print("\n--- against optimizer_global.py: one model, one train ---")
    unbounded = build_and_solve(payloads, objective="lexicographic")
    check("unbounded solve", unbounded.status, "OPTIMAL")
    if unbounded.feasible:
        held = unbounded.total_hold_s.get("40208", 0)
        parts = {
            resource_id: unbounded.slack_s[("40208", resource_id)]
            for resource_id in SECTIONS
            if ("40208", resource_id) in unbounded.slack_s
        }
        print(f"  slack by resource: {parts}")
        print(f"  total_hold[40208] = {held}s")
        check("total_hold is the sum of the per-hop slacks",
              held, sum(parts.values()))
        check("and it exceeds the ceiling a per-conflict solve enforced",
              held > MAX_HOLD_S, True)

    bounded = build_and_solve(
        payloads, objective="lexicographic", hold_bound=MAX_HOLD_S
    )
    print(f"  with total_hold[t] <= {MAX_HOLD_S}s: {bounded.status}")
    check("the global model refuses the composed plan",
          bounded.feasible and bounded.total_hold_s.get("40208", 0) <= MAX_HOLD_S
          or not bounded.feasible,
          True)
    if bounded.feasible:
        check("...by holding less, not by ignoring the bound",
              bounded.total_hold_s.get("40208", 0) <= MAX_HOLD_S, True)

    print("\nFAIL: " + "; ".join(failures[:12]) if failures else "\nPASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())