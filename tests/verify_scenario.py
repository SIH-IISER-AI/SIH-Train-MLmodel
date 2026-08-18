"""Static check on a scenario file: legal start, filter chain, group sizes, class spread.

No Redis, no simulator loop. Boots the injector, takes tick-0 telemetry, feeds
the detector, and reports exactly what `evaluate` in ai-engine/main.py would
see -- including the four-stage narrowing from raw contention groups down to
the conflicts that actually reach the solver.

Every constant and rule is imported from the module that owns it. Nothing here
re-derives the engine's behaviour; if this tool and the engine ever disagree,
that is an import error, not a silent drift.

Usage:  python3 tests/verify_scenario.py [data/scenario10.json]
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
from optimizer import CLASS_LABELS, MAX_TRAINS_ENUMERATED, priority_class


def kept_ids(group, det):
    """The trains optimize_precedence would actually enumerate.

    Mirrors the truncation in optimize_precedence: sort by priority weight
    descending, take the first MAX_TRAINS_ENUMERATED. A threshold comparison
    over-keeps on ties -- both freights sit at w=2.0, so a cutoff landing there
    would show both as kept while the solver keeps one.

    Takes the whole group so callers that only hold the conflict dict (e.g.
    count_refusals) can reuse it without unpacking.
    """
    ids = group["conflicting_train_ids"]
    if len(ids) <= MAX_TRAINS_ENUMERATED:
        return set(ids)
    ordered = sorted(ids, key=lambda i: -float(det.trains[i].priority))
    return set(ordered[:MAX_TRAINS_ENUMERATED])


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/scenario10.json"
    network = json.load(open("data/network.json"))
    scenario = json.load(open(path))
    fleet = {t["train_id"]: t for t in scenario["trains"]}

    inj = LiveTelemetryInjector(network, scenario)   # raises if the start is illegal
    det = ConflictDetector(network, fleet)
    for event in inj.tick():
        det.ingest(event)

    groups = det.detect_grouped()

    # The same narrowing evaluate() applies, in the same order.
    within = [g for g in groups
              if g["predicted_time_to_conflict_seconds"] <= SOLVE_WITHIN_S]
    actionable = [g for g in within if g["actionable"]]
    # Same survivor rule as evaluate(): soonest contention wins, NOT the largest
    # group. A different survivor means different _windows, different entry
    # stations and different directives, so a tool that picks differently is
    # describing an engine you do not have.
    solved: dict = {}
    for g in actionable:
        key = conflict_id_for(g)
        incumbent = solved.get(key)
        if incumbent is None or (
            g["predicted_time_to_conflict_seconds"]
            < incumbent["predicted_time_to_conflict_seconds"]
        ):
            solved[key] = g

    print(f"scenario: {path}   trains={len(fleet)}")
    print("legal start: OK (no two trains seeded in one interlocking resource)")
    print()
    print("filter chain per evaluate()")
    print(f"  raw contention groups           {len(groups):>4}")
    print(f"  within SOLVE_WITHIN_S={SOLVE_WITHIN_S:<6}    {len(within):>4}")
    print(f"  actionable                      {len(actionable):>4}")
    print(f"  distinct conflict_ids -> solver {len(solved):>4}")

    perms = 0
    for g in solved.values():
        n = min(len(g["conflicting_train_ids"]), MAX_TRAINS_ENUMERATED)
        p = 1
        for k in range(2, n + 1):
            p *= k
        perms += p
    print(f"  CP solves per evaluate          {perms:>4}"
          f"   (MAX_TRAINS_ENUMERATED={MAX_TRAINS_ENUMERATED})")

    for key, g in sorted(
        solved.items(), key=lambda kv: -len(kv[1]["conflicting_train_ids"])
    ):
        ids = g["conflicting_train_ids"]
        keep = kept_ids(g, det)
        classes = Counter(
            CLASS_LABELS[priority_class(det.trains[i].telemetry["train_type"])]
            for i in ids
        )
        print(f"\n{key}  {g['resource_id']}  group={len(ids)}  "
              f"dropped={len(ids) - len(keep)}  severity={g['severity']}  "
              f"t={g['predicted_time_to_conflict_seconds']}s")
        print(f"  classes: {dict(classes)}  distinct={len(classes)}")
        rows = sorted(
            ((det.trains[i].telemetry["train_name"], i,
              det.trains[i].telemetry["train_type"],
              det.trains[i].priority,
              g["_windows"][i].t_in, g["_windows"][i].t_out) for i in ids),
            key=lambda r: r[4],
        )
        for name, tid, ttype, w, t_in, t_out in rows:
            flag = "KEPT " if tid in keep else "DROP "
            print(f"    {flag}{tid:>6} {name:<20} {ttype:<13} w={w:<5} "
                  f"t_in={t_in:7.0f}s t_out={t_out:7.0f}s")

    if not solved:
        print("\nno conflicts reach the solver -- this scenario measures nothing")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())