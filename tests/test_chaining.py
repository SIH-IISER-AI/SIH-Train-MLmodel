"""Day-9 gate: chaining, and the stop penalty on the following run.

Three cases, as a file rather than by eye. Synthetic payloads, no detector and
no injector: the question here is whether the ARITHMETIC of the chain is exact,
and a scenario-driven test answers it only for whatever numbers the scenario
happens to produce.

  (a) no stop        -> chained entry equals the unimpeded projection, exactly
  (b) one forced stop -> the train is brought to a stand at the entry to R1
  (c) the raise       -> entry[R2] rises by EXACTLY occupancy_from_stop_s minus
                         occupancy_running_s FOR R1, not for R2 and not within
                         a tolerance

(c) is the only one that catches an off-by-one in k, and it can only do so if
the two resources produce DIFFERENT penalties. Length does not achieve that:

    from_stop - running  =  (vt/a + (L-d)/vt) - ((vt-v0)/a + (L-d')/vt)

and L cancels once both runs clear the acceleration distance. The penalty is a
property of the SPEED PROFILE, not of the resource -- 4000 m and 9000 m at the
same line speed both give 83 s for this rake. So R2 carries a LINE SPEED
restriction instead, which is what actually varies along your section
(60/110/100/130/100/130/75/130/60). 100 km/h gives 83 s, 60 km/h gives 70 s.

Usage:  python3 tests/test_chaining.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "shared")
sys.path.insert(0, "ai-engine")

from optimizer import _prepare
from optimizer_global import build_and_solve, chain_links

R1 = "TRK-UP-MAIN|BLK-201U"
R2 = "TRK-UP-MAIN|BLK-202U"

#: Length varies the OCCUPANCY (so NoOverlap and exit differ between the two
#: resources) but NOT the stop penalty -- see the module docstring.
R1_LENGTH_M = 4000.0
R2_LENGTH_M = 9000.0

#: This is the discriminator. R2 is a speed-restricted block, so its stop
#: penalty is a different number from R1's and reaching for the wrong k is
#: visible rather than accidentally right.
R1_LINE_SPEED_KMH = 100.0
R2_LINE_SPEED_KMH = 60.0

ENTRY_R1_S = 60      # unimpeded arrival at R1
ENTRY_R2_S = 260     # unimpeded arrival at R2 -> travel over R1 is 200 s
BLOCKER_ENTRY_S = 400

failures = []


def check(what, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got} (want {want})")
    if not ok:
        failures.append(what)


def topology(resource_id: str, length_m: float, line_speed_kmh: float):
    return {
        "block_id": resource_id,
        "resource_id": resource_id,
        "junction_id": "PWL",
        "single_line": False,
        "length_m": length_m,
        "line_speed_kmh": line_speed_kmh,
        "headway_seconds": 120,
        "max_regulation_seconds": 300,
        "max_hold_seconds": 900,
    }


def freight(resource_id: str, projected_entry_s: int, distance_m: float):
    """The train under test. Loop available, so a stand is a loop berth."""
    return {
        "train_id": "40999",
        "train_name": "BOXN Test Rake",
        "train_type": "FREIGHT",
        "current_speed": 60.0,
        "target_speed_kmh": 75.0,
        "max_speed_kmh": 75.0,
        "distance_to_bottleneck": distance_m,
        "projected_entry_s": float(projected_entry_s),
        "priority_weight": 2.0,
        "train_length_m": 700.0,
        "existing_delay_seconds": 0,
        "hold_station_id": "PWL",
        "hold_loop_id": "LOOP-PWL-TEST",
        "hold_loop_length_m": 800.0,
        "direction": "UP",
    }


def blocker():
    """A premier service occupying R1 long enough to force a stand."""
    return {
        "train_id": "12002",
        "train_name": "Bhopal Shatabdi",
        "train_type": "SHATABDI",
        "current_speed": 100.0,
        "target_speed_kmh": 130.0,
        "max_speed_kmh": 130.0,
        "distance_to_bottleneck": 11000.0,
        "projected_entry_s": float(BLOCKER_ENTRY_S),
        "priority_weight": 9.0,
        "train_length_m": 500.0,
        "existing_delay_seconds": 0,
        "hold_station_id": "PWL",
        "hold_loop_id": None,
        "hold_loop_length_m": 0.0,
        "direction": "DOWN",
    }


def main() -> int:
    top_r1 = topology(R1, R1_LENGTH_M, R1_LINE_SPEED_KMH)
    top_r2 = topology(R2, R2_LENGTH_M, R2_LINE_SPEED_KMH)
    payload_a = {
        R1: ([freight(R1, ENTRY_R1_S, 1000.0)], top_r1),
        R2: ([freight(R2, ENTRY_R2_S, 5000.0)], top_r2),
    }
    payload_b = {
        R1: ([freight(R1, ENTRY_R1_S, 1000.0), blocker()], top_r1),
        R2: ([freight(R2, ENTRY_R2_S, 5000.0)], top_r2),
    }

    prepared_r1 = {t.train_id: t for t in _prepare(*payload_b[R1])}
    prepared_r2 = {t.train_id: t for t in _prepare(*payload_a[R2])}
    at_r1 = prepared_r1["40999"]
    at_r2 = prepared_r2["40999"]

    raise_r1 = at_r1.occupancy_from_stop_s - at_r1.occupancy_running_s
    raise_r2 = at_r2.occupancy_from_stop_s - at_r2.occupancy_running_s
    print(f"stop penalty on the run out of R1: {raise_r1}s "
          f"(R2 would be {raise_r2}s -- different on purpose)\n")
    check("the two penalties differ, so case (c) can discriminate",
          raise_r1 != raise_r2, True)

    links = {(l.train_id, l.from_resource): l for l in chain_links(
        {r: _prepare(*p) for r, p in payload_b.items()}
    )}
    hop = links.get(("40999", R1))
    check("link R1 -> R2 exists", hop is not None, True)
    if hop is None:
        print("\nFAIL: no chain link built")
        return 1
    check("travel_s is the projection delta",
          hop.travel_s, ENTRY_R2_S - ENTRY_R1_S)
    check("stop_extra_s is R1's from_stop minus running",
          hop.stop_extra_s, raise_r1)

    # ---- (a) no stop -----------------------------------------------------
    print("\n--- (a) unimpeded: chained entry must equal the projection ---")
    sol_a = build_and_solve(payload_a, objective="flat")
    check("solved", sol_a.status, "OPTIMAL")
    if sol_a.feasible:
        check("entry R1", sol_a.entry_s[("40999", R1)], ENTRY_R1_S)
        check("entry R2", sol_a.entry_s[("40999", R2)], ENTRY_R2_S)
        check("slack R1", sol_a.slack_s[("40999", R1)], 0)
        check("slack R2", sol_a.slack_s[("40999", R2)], 0)
        check("not stopped R1", sol_a.stopped[("40999", R1)], False)
        check("total_hold", sol_a.total_hold_s.get("40999"), 0)

    # ---- (b) one forced stop --------------------------------------------
    print("\n--- (b) premier pinned ahead: the freight must stand at R1 ---")
    sol_b = build_and_solve(
        payload_b, pin_order={R1: ["12002", "40999"]}, objective="flat"
    )
    check("solved", sol_b.status, "OPTIMAL")
    if not sol_b.feasible:
        print("\nFAIL: " + "; ".join(failures))
        return 1

    blocker_exit = sol_b.exit_s[("12002", R1)]
    check("freight enters R1 one headway after the premier clears",
          sol_b.entry_s[("40999", R1)], blocker_exit + 120)
    check("freight is brought to a stand", sol_b.stopped[("40999", R1)], True)
    check("in a loop, not on the main", sol_b.in_loop[("40999", R1)], True)

    # ---- (c) the raise is EXACTLY that difference ------------------------
    print("\n--- (c) the raise on the following run, exactly ---")
    entry_r1 = sol_b.entry_s[("40999", R1)]
    entry_r2 = sol_b.entry_s[("40999", R2)]
    check("entry[R2] == entry[R1] + travel + stop_extra",
          entry_r2, entry_r1 + hop.travel_s + raise_r1)
    check("the raise attributable to standing",
          entry_r2 - entry_r1 - hop.travel_s, raise_r1)
    check("it is NOT R2's penalty",
          entry_r2 - entry_r1 - hop.travel_s == raise_r2, False)

    # ---- issue 3: the hold is charged once, where it was imposed ---------
    print("\n--- (d) no double counting: slack at R2 is zero ---")
    check("slack R2 after an upstream hold", sol_b.slack_s[("40999", R2)], 0)
    check("wait R2 is cumulative lateness, not a second hold",
          sol_b.wait_s[("40999", R2)], entry_r2 - ENTRY_R2_S)
    check("total_hold equals the single hold at R1",
          sol_b.total_hold_s.get("40999"), sol_b.slack_s[("40999", R1)])
    check("and the train is not stopped a second time",
          sol_b.stopped[("40999", R2)], False)

    print("\nFAIL: " + "; ".join(failures[:12]) if failures else "\nPASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())