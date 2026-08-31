"""Headless A/B harness: run the enumerate engine against itself, with and
without the controller pressing Approve.

No Redis, no sleep, no wall-clock pacing. One process drives the whole loop:

    inj.tick()            -> telemetry for every train
    det.ingest()          -> spatial-temporal projection
    solvable_conflicts()  -> the same filter chain evaluate() applies
    optimize_precedence() -> CP-SAT enumeration, OPT-1 first
    inj.submit_directive()-> arm A only

Two arms, identical in every other respect:

    A  enumerate + approve      the engine is allowed to act
    B  enumerate + no approve   the engine runs and is measured, and the
                                fleet is left to the simulator's greedy
                                priority rule

Arm B is not "engine off". The solver still runs and every audit still fires,
so the two arms are comparable row by row; the only difference is whether the
directives reach the injector.

Usage:
    python3 tests/harness.py --seed 1 --arm A
    python3 tests/harness.py --seed 0 --arm B --ticks 60 --csv /tmp/smoke.csv

Seed 0 means NO perturbation, i.e. data/scenario10.json exactly as committed.
That is the row to compare against the day-2 report; seeds 1..N are perturbed
and will not reproduce those numbers.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, "shared")
sys.path.insert(0, "simulator")
sys.path.insert(0, "ai-engine")

import optimizer  # noqa: E402
from detector import ConflictDetector  # noqa: E402
from injector import LiveTelemetryInjector  # noqa: E402
from main import plan_fingerprint, solvable_conflicts  # noqa: E402
from optimizer import CLASS_PREMIER, optimize_precedence, priority_class  # noqa: E402

ENGINE = os.getenv("ENGINE", "enumerate").strip().lower()
if ENGINE not in ("enumerate", "global"):
    raise SystemExit(f"ENGINE={ENGINE!r}; expected 'enumerate' or 'global'")
optimize_global = None
if ENGINE == "global":
    from optimizer_global import optimize_global  # noqa: E402,F811

sys.path.insert(0, "tests")
from count_refusals import audit_plans  # noqa: E402

# ---------------------------------------------------------------------------
# Determinism overrides.
#
# Two production settings are wall-clock deadlines, and a wall-clock deadline
# makes the RESULT depend on machine load, not just the timing column:
#
#   ENUMERATION_BUDGET_S  breaks out of the permutation loop after N seconds,
#                         so a loaded machine enumerates fewer orders and can
#                         return a different OPT-1.
#   SOLVER_TIME_LIMIT_S   caps each CP-SAT solve; a solve that hits the limit
#                         returns FEASIBLE rather than OPTIMAL.
#
# Both are lifted here so that step 8's "every column must match" is a
# statement about the engine rather than about the machine. Neither is a
# working budget in this model class -- day-1 measured ~1.7 ms per solve
# against a 250 ms limit -- so lifting them changes no answer that was not
# already wrong. Recorded in the run's .env.txt so a baseline can never be
# mistaken for a live-demo configuration.
# ---------------------------------------------------------------------------
optimizer.ENUMERATION_BUDGET_S = float(
    os.getenv("HARNESS_ENUMERATION_BUDGET_S", "1e9")
)
optimizer.SOLVER_TIME_LIMIT_S = float(
    os.getenv("HARNESS_SOLVER_TIME_LIMIT_S", "5.0")
)
#: Binds before the wall-clock limit above, so the truncation point is a
#: property of the model rather than of the machine. 60 s of wall clock was
#: reproducible in principle and unbounded in practice: one hard conflict at
#: 120 permutations is two hours.
optimizer.SOLVER_DETERMINISTIC_TIME = float(
    os.getenv("HARNESS_SOLVER_DETERMINISTIC_TIME", "1.0")
)

NETWORK_PATH = os.getenv("NETWORK_PATH", "data/network.json")
SCENARIO_PATH = os.getenv("SCENARIO_PATH", "data/scenario10.json")

#: 1080 ticks x (2.0 s x 5) = 10800 sim-seconds = 3 sim-hours.
DEFAULT_TICKS = int(os.getenv("HARNESS_TICKS", "1080"))

#: The bottleneck the throughput column counts clearances through.
THROUGHPUT_RESOURCE = os.getenv("THROUGHPUT_RESOURCE", "SEC-PWL-KSV")

KM_JITTER = float(os.getenv("SEED_KM_JITTER", "2.0"))
DELAY_JITTER = float(os.getenv("SEED_DELAY_JITTER", "300"))
MAX_SEED_ATTEMPTS = int(os.getenv("SEED_MAX_ATTEMPTS", "50"))

# ---------------------------------------------------------------------------
# APPROVAL RULE -- the design decision that determines every number below.
#
# The same conflict is re-detected on every tick. Approving OPT-1 each time
# would queue the same HOLD/REGULATE 1080 times, reset hold_expires_sim_s on
# every one of them, and turn the delay columns into an artefact of the tick
# rate. So an approval fires at most once per identity, and the identity is
# chosen here:
#
#   conflict_id   (DEFAULT) approve once per conflict_id and never again.
#                 This is what a controller does: you decide a crossing once.
#                 Note what conflict_id actually is -- conflict_id_for() hashes
#                 the sorted TRAIN SET, not the resource. A train joining or
#                 leaving the contention therefore mints a new id and earns a
#                 new approval. That is deliberate and matches evaluate(),
#                 which drops plan_in_force when train_ids change: a different
#                 train set is a different decision. It does mean "once" is not
#                 literally once over a 3-hour run, which is why approval_events
#                 is a CSV column rather than an assumption.
#
#   fingerprint   approve again whenever plan_fingerprint(OPT-1) changes,
#                 mirroring the IN_FORCE/DIVERGED logic in evaluate(). Closer
#                 to production, noisier, and it re-arms every hold timer on
#                 each re-approval.
#
# conflict_id is the default because a harness should measure one decision per
# decision. Both are kept so the choice itself can be A/B'd rather than
# asserted, but a CSV mixing the two rules is not a comparison -- the rule is
# recorded per row.
# ---------------------------------------------------------------------------
APPROVAL_RULE = os.getenv("APPROVAL_RULE", "conflict_id")

CSV_COLUMNS = [
    "seed",
    "arm",
    "approval_rule",
    "ticks",
    "premier_delay_s",
    "total_fleet_delay_s",
    f"cleared_through_{THROUGHPUT_RESOURCE}_per_sim_hour",
    "max_solve_ms",
    "largest_group",
    # t0 = one evaluate at tick 1, the same measurement tests/count_refusals.py
    # prints. These are the numbers comparable to the day-2 report.
    "uncovered_trains_t0",
    "contradictory_instructions_t0",
    "refused_directives_t0",
    "policy_exceeded_t0",
    # _total = summed over every approval event in the run. A different
    # statistic with a different meaning; naming them apart is the only thing
    # stopping day 14 from comparing one against the other.
    "uncovered_trains_total",
    "contradictory_instructions_total",
    "refused_directives_total",
    "policy_exceeded_total",
    "approval_events",
    "distinct_conflict_ids",
    "directives_submitted",
    "seed_attempts",
]

#: Wall-clock columns cannot be expected to reproduce. Step 8 compares
#: everything except these.
NONDETERMINISTIC_COLUMNS = ("max_solve_ms",)


def perturb(base_scenario: dict, rng: random.Random) -> dict:
    """One draw: shift every train's start and initial delay.

    start_offset_km is clamped at 0 rather than resampled, because a negative
    start is not a near miss -- it is off the route.
    """
    scenario = json.loads(json.dumps(base_scenario))
    for train in scenario["trains"]:
        km = float(train.get("start_offset_km", 0.0))
        delay = float(train.get("initial_delay_seconds", 0))
        train["start_offset_km"] = max(
            0.0, round(km + rng.uniform(-KM_JITTER, KM_JITTER), 3)
        )
        train["initial_delay_seconds"] = max(
            0, int(round(delay + rng.uniform(-DELAY_JITTER, DELAY_JITTER)))
        )
    return scenario


def build_injector(base_scenario: dict, network: dict, seed: int):
    """Return (injector, scenario, attempts).

    LiveTelemetryInjector.__init__ raises on an illegal start -- two trains
    seeded inside one interlocking resource. On a 4 km block grid with trains
    5-13 km apart, a +/-2 km jitter collides often enough that a retry is
    mandatory rather than defensive. The retry re-draws from the SAME RNG
    stream, so seed N always yields the same accepted scenario.
    """
    if seed == 0:
        return LiveTelemetryInjector(network, base_scenario), base_scenario, 1

    rng = random.Random(seed)
    last_error = None
    for attempt in range(1, MAX_SEED_ATTEMPTS + 1):
        scenario = perturb(base_scenario, rng)
        try:
            return LiveTelemetryInjector(network, scenario), scenario, attempt
        except ValueError as exc:
            last_error = exc
    raise SystemExit(
        f"seed {seed}: no legal start in {MAX_SEED_ATTEMPTS} draws "
        f"(last: {last_error}). KM_JITTER={KM_JITTER} is too wide for this "
        f"scenario's train spacing."
    )


def _fires(conflict_id: str, directives, seen: dict) -> bool:
    """Has this decision already been made? See APPROVAL RULE above."""
    if APPROVAL_RULE == "fingerprint":
        fingerprint = plan_fingerprint(directives)
        if seen.get(conflict_id) == fingerprint:
            return False
        seen[conflict_id] = fingerprint
        return True
    if APPROVAL_RULE != "conflict_id":
        raise SystemExit(
            f"APPROVAL_RULE={APPROVAL_RULE!r}; expected 'conflict_id' or "
            f"'fingerprint'"
        )
    if conflict_id in seen:
        return False
    seen[conflict_id] = True
    return True


def run(seed: int, arm: str, ticks: int, progress_every: int = 0) -> dict:
    network = json.load(open(NETWORK_PATH))
    base_scenario = json.load(open(SCENARIO_PATH))

    inj, scenario, attempts = build_injector(base_scenario, network, seed)
    fleet = {t["train_id"]: t for t in scenario["trains"]}
    det = ConflictDetector(network, fleet)

    approve = arm.upper() == "A"
    seen: dict = {}
    all_conflict_ids: set = set()

    max_solve_s = 0.0
    solve_calls = 0
    solve_total_s = 0.0
    largest_group = 0
    approval_events = 0
    directives_submitted = 0
    totals = {
        "uncovered_trains": 0,
        "contradictory_instructions": 0,
        "refused": 0,
        "policy_exceeded": 0,
    }
    t0_summary = None

    last_resource: dict = {}
    cleared = 0

    started = time.perf_counter()
    for tick in range(1, ticks + 1):
        events = inj.tick()
        for event in events:
            det.ingest(event)
            # A clearance is a transition OUT of the bottleneck, counted per
            # train. Counting occupancy instead would count the same train once
            # per tick it spends inside.
            train_id = event["train_id"]
            resource = event["resource_id"]
            if (last_resource.get(train_id) == THROUGHPUT_RESOURCE
                    and resource != THROUGHPUT_RESOURCE):
                cleared += 1
            last_resource[train_id] = resource

        candidates, _counts = solvable_conflicts(det)
        all_conflict_ids.update(candidates)

        global_plans: dict = {}
        if ENGINE == "global" and candidates:
            solve_started = time.perf_counter()
            try:
                global_plans = optimize_global(det, candidates, max_scenarios=1)
            except Exception as exc:  # noqa: BLE001
                print(f"  tick {tick}: global solve failed: {exc!r}", flush=True)
                global_plans = {}
            elapsed = time.perf_counter() - solve_started
            max_solve_s = max(max_solve_s, elapsed)
            solve_calls += 1
            solve_total_s += elapsed

        fired: dict = {}
        scenarios_by_conflict: dict = {}
        for conflict_id, conflict in candidates.items():
            largest_group = max(
                largest_group, len(conflict["conflicting_train_ids"])
            )

            if ENGINE == "global":
                scenarios = global_plans.get(conflict_id, [])
            else:
                payload_trains, payload_topology = det.optimiser_inputs(conflict)
                solve_started = time.perf_counter()
                scenarios = optimize_precedence(payload_trains, payload_topology)
                elapsed = time.perf_counter() - solve_started
                max_solve_s = max(max_solve_s, elapsed)
                solve_calls += 1
                solve_total_s += elapsed

            scenarios_by_conflict[conflict_id] = scenarios
            if not scenarios:
                continue

            directives = scenarios[0].get("directives", [])
            if not _fires(conflict_id, directives, seen):
                continue

            fired[conflict_id] = conflict
            approval_events += 1
            if approve:
                for directive in directives:
                    if inj.submit_directive(directive):
                        directives_submitted += 1

        # tick 1 is the same state count_refusals.py audits, so audit ALL of
        # it once here rather than only the conflicts that fired.
        if tick == 1:
            t0_summary = audit_plans(
                candidates, scenarios_by_conflict, det, inj
            )["summary"]

        if fired:
            summary = audit_plans(
                fired, scenarios_by_conflict, det, inj
            )["summary"]
            totals["uncovered_trains"] += summary["uncovered_trains"]
            totals["contradictory_instructions"] += summary[
                "contradictory_instructions"
            ]
            totals["refused"] += summary["refused"]
            totals["policy_exceeded"] += summary["policy_exceeded"]

        if progress_every and tick % progress_every == 0:
            wall = time.perf_counter() - started
            print(
                f"  tick {tick:>5}/{ticks}  conflicts={len(candidates)} "
                f"approvals={approval_events} cleared={cleared} "
                f"solves={solve_calls} solve_s={solve_total_s:.0f} "
                f"wall={wall:.0f}s  proj={wall * ticks / tick:.0f}s",
                flush=True,
            )

    premier = 0
    fleet_total = 0
    for train in inj.trains.values():
        delay = inj._delay_seconds(train)
        fleet_total += delay
        if priority_class(train.train_type) == CLASS_PREMIER:
            premier += delay

    # Opt-in, prints nothing by default, changes no column. A fleet-delay
    # regression spread evenly across ten trains is the priority trade working
    # as designed; the same number concentrated in two trains is a hold that
    # never released, and those need different responses.
    if os.getenv("HARNESS_PER_TRAIN"):
        for t in sorted(inj.trains.values(),
                        key=lambda x: -inj._delay_seconds(x)):
            print(f"  {t.train_id:>6} {t.train_type:<14} "
                  f"class={priority_class(t.train_type)} "
                  f"delay={inj._delay_seconds(t)}s")

    sim_hours = inj.elapsed_sim_seconds / 3600.0
    t0 = t0_summary or {
        "uncovered_trains": 0, "contradictory_instructions": 0,
        "refused": 0, "policy_exceeded": 0,
    }

    return {
        "seed": seed,
        "arm": arm.upper(),
        "approval_rule": APPROVAL_RULE,
        "ticks": ticks,
        "premier_delay_s": premier,
        "total_fleet_delay_s": fleet_total,
        f"cleared_through_{THROUGHPUT_RESOURCE}_per_sim_hour": round(
            cleared / sim_hours if sim_hours else 0.0, 3
        ),
        "max_solve_ms": round(max_solve_s * 1000.0, 1),
        "largest_group": largest_group,
        "uncovered_trains_t0": t0["uncovered_trains"],
        "contradictory_instructions_t0": t0["contradictory_instructions"],
        "refused_directives_t0": t0["refused"],
        "policy_exceeded_t0": t0["policy_exceeded"],
        "uncovered_trains_total": totals["uncovered_trains"],
        "contradictory_instructions_total": totals["contradictory_instructions"],
        "refused_directives_total": totals["refused"],
        "policy_exceeded_total": totals["policy_exceeded"],
        "approval_events": approval_events,
        "distinct_conflict_ids": len(all_conflict_ids),
        "directives_submitted": directives_submitted,
        "seed_attempts": attempts,
    }


def append_row(path: str, row: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists() or target.stat().st_size == 0
    with target.open("a", newline="") as handle:
        # LF, not the csv module's default CRLF: this file gets committed to
        # docs/baselines/ and a \r on every line makes each rerun look like a
        # whole-file diff.
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS,
                                lineterminator="\n")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True,
                        help="0 = unperturbed scenario as committed")
    parser.add_argument("--arm", choices=["A", "B", "a", "b"], required=True)
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--csv", default="docs/baselines/ab-enumerate.csv")
    parser.add_argument("--progress", type=int, default=120,
                        help="print a line every N ticks; 0 to silence")
    args = parser.parse_args()

    print(
        f"harness seed={args.seed} arm={args.arm.upper()} ticks={args.ticks} "
        f"engine={ENGINE} rule={APPROVAL_RULE} cap={optimizer.MAX_TRAINS_ENUMERATED} "
        f"budget={optimizer.ENUMERATION_BUDGET_S} "
        f"limit={optimizer.SOLVER_TIME_LIMIT_S}",
        flush=True,
    )
    started = time.perf_counter()
    row = run(args.seed, args.arm, args.ticks, args.progress)
    append_row(args.csv, row)

    print(f"done in {time.perf_counter() - started:.1f}s -> {args.csv}")
    for column in CSV_COLUMNS:
        print(f"  {column:<44}{row[column]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())