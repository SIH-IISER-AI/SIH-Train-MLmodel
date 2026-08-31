# Harness notes

Instrument findings. Not results — the properties of the measuring apparatus
that had to be discovered before any result could be trusted. Every entry here
cost time; none is obvious from the code.

## sys.path order is load-bearing

`simulator/main.py` and `ai-engine/main.py` both exist. Every test does three
separate inserts:

    sys.path.insert(0, "shared")
    sys.path.insert(0, "simulator")
    sys.path.insert(0, "ai-engine")

which reverses to `ai-engine, simulator, shared` — so `from main import
solvable_conflicts` resolves to the AI engine. A single
`sys.path[:0] = ["shared", "simulator", "ai-engine"]` puts them in the opposite
order and silently imports the simulator's main instead. Diagnosed once from a
`ModuleNotFoundError: redis` raised from the wrong file.

## Wall-clock budgets make results a function of machine load

Enumerate: 5,040 permutations at ~2.8 ms is ~14 s against
`ENUMERATION_BUDGET_S = 5.0`. It explores roughly a third of the space, and
which third depends on what else the laptop is doing. Two identical cap-7 runs
returned different OPT-1 orders.

The global engine reproduced this while its per-tier budget was too small: at
0.8 s/tier three identical runs gave 3/6, 2/6 and 6/6 tiers with worst holds of
15,178 / 16,720 / 14,722 s. Fixed by raising the budget until the descent
completes; `GLOBAL_DET_BUDGET` sets a deterministic-time ceiling, which is
reproducible across machines, and the wall clock is only a safety net.

Any timing-sensitive result needs three identical runs before it is reported.

## CP-SAT carries no state between Solve() calls

Every `Solve()` re-runs full presolve on the whole model. Solution hinting
(`AddHint`) helps search and does nothing for that fixed cost. Consequence: a
budget divided across N tiers puts each tier below the setup cost as N grows.
At 0.15 s/tier the lexicographic descent completed 1 of 6 at every total from
0.5 s to 2.0 s. Budgets are allocated PER TIER for this reason.

Observed per-tier cost on scenario10 tick 0 (74 intervals, 214 booleans) is
500-1300 ms and roughly flat across tiers. No tier is cheaper for having been
warm-started.

## A truncated descent reports the first tier's status

`solver.Solve()` returns per call. If tier 0 is OPTIMAL and tier 1 times out,
the naive return status is OPTIMAL and nothing says the plan is missing four
tiers. `counts["tiers_completed"]`, `counts["truncated"]` and
`counts["tier_log"]` exist because of this. Check them, not the status.

## PYTHONHASHSEED and dict ordering

Set orderings leak into output where a tie-break is missing. Every comparison
in the encoding gate sorts explicitly; `chain_links` sorts on
`(earliest_arrival_s, resource_id)` and `_headline` on `(entry_s, train_id)`
for the same reason. If a result changes between runs on one machine, look for
an unsorted set before suspecting the solver.

## The detector's horizon is global mutable state

`scope_window()` mutates `detector.horizon_seconds` and clears
`_projection_cache`. It restores both in a `finally`. Leaving them mutated
silently changes every subsequent `detect()` in the same process — which is
what a sizing sweep across four horizons does if it forgets.

## Tick arithmetic

`injector.sim_seconds_per_tick` is the step, not the wall clock. Tests that
watch for a duration should compare `injector.elapsed_sim_seconds` against a
sim-seconds target rather than counting ticks; the tick length is configurable
and two tests already disagree about it.

## Refused directives are silent

`_drain_directives()` drops a refused directive with `continue`. There is no
counter and no log line. The only way to detect a refusal is to check whether
the intent appears on the train afterwards — `hold_station_id` for a hold,
`regulated_to_kmh` for a regulation. `tests/test_directive_replay.py` does
this; nothing else does.

Re-targeting is NOT refusal and must not be tested as such. When the named
station is astern the injector moves a HOLD_AT_LOOP to the next loop ahead:
`hold_station_id` is set, just not to what was asked. Detect it by comparing
the landed station to the requested one, and report it — the directive was
executed, at a place the model did not price.

## Filter-chain equivalence

Any tool that selects conflicts must call `solvable_conflicts()` rather than
reimplementing the filter. A tool that reimplemented it drifted from the engine
and produced a conflict count that matched nothing else in the repo. If a
number in `docs/` disagrees with a number from a test, check this first.

## Report files

`run_all.sh` writes a dated report. These accumulate one per debugging round
and should not be committed — add `run-report-*.txt` to `.gitignore` and keep
the one that backs a claim.

## A hold can be addressed to a station the train has passed

`optimiser_inputs` sets `hold_station_id = window.entry_station_id` -- the
station governing the contested RESOURCE. For a block-level resource that is
the station at the head of the whole link, which can be several blocks astern
even when the block itself is ahead.

Observed on scenario10 tick 0:
  - STAND_ON_MAIN refused outright. 12626 at BVH from 44.53 km.
  - HOLD_AT_LOOP re-targeted by the injector to the next loop ahead. The train
    IS held; the model priced it at a different loop. 12280, MTJ -> KSV.

Do NOT fix this by repointing hold_station_id at "the first station ahead of
the train". That was implemented and reverted. hold_station_id also feeds
`topology.loop_at()`, so repointing it destroyed loop availability for most
trains and made BLK-115D, BLK-126U and BLK-127U INFEASIBLE -- a train that
must give way could neither berth nor stand. The directive set fell from 8 to
7 and 12626/12280/40208 acquired large negative model-vs-observed deltas: the
plan stopped instructing trains it had priced holds for.

The right fix separates the two roles. Keep the loop lookup on the resource's
governing station; add a SEPARATE `stand_station_id`, the last station at or
before the resource entry that the train has not yet passed, None when there
is none -- in which case a stand is genuinely impossible on that approach and
only regulation can shed time. Own gate, own day.

## HOLD_MIN_APPROACH_M is an emission filter, not a model constraint

Applied as `model.Add(stopped[key] == 0)` it perturbs schedules that do not
depend on the forbidden decision. Measured on TRK-DOWN-MAIN|BLK-108D at
scenario10 tick 0, pinned order 12050 -> 20172:

    with the constraint    20172 enters at 344 s
    without it             20172 enters at 245 s   (= _solve_order)

12050 is not stopped in either solution and its exit is 125 s in both, so the
99 s is not the stand being priced. UNEXPLAINED: with stopped[12050] forced to
0, 245 s remains feasible and strictly cheaper, yet the solver reports OPTIMAL
at 344 s. Someone's mental model of the model is wrong. The probe that found
it is in the day-11 transcript; re-run it before touching the reachability
logic again.

A stand the model prices but cannot emit is a costed decision the card does
not show. That is the cheaper error.

## "Discharged" is not a predicate anyone has read

Three predicates for "the hold has discharged" were tried in
tests/test_directive_replay.py and all three reported failure on trains whose
expiry the injector had set correctly (12002: release_timeout 4273 s,
hold_expires_sim_s 4283 s at now=20 s, watch 18,322 s):

    hold_station_id is None and in_loop is None
    hold_station_id is None
    the expiry firing

The flag is evidently cleared and re-set, or cleared on a condition not in any
of the above. Until someone reads the injector's release path and writes the
post-condition down, discharge is PRINTED and not asserted.

## ast.parse is not a pre-flight check

`python3 -c "ast.parse(...)"` was the standard pre-flight all through day 11
and it cannot catch an undefined name. A `hold_loop` / `loop` rename slipped
through it and cost a round. Use pyflakes, or import the module.

## The suite went green twice while the engine got worse

Day 11, twice: the convex tier priced per interval let the model buy a cheaper
objective with an extra stop, and repointing `hold_station_id` cut the
directive set from 8 to 7. Both times the failing test count did not rise.

The tests that caught them were the ones that RUN the plan and print numbers:
the directive-replay gate, and the model-vs-simulator delta table. The tests
that did not were the ones comparing the engine against itself.

Corollary for the delta table: a large NEGATIVE delta — the simulator standing
a train LESS than the model priced — means the plan was not executed as
costed. That is the signal. Positive deltas are the greedy authority and are
expected.

## `policy_exceeded` is overloaded and is not a ladder indicator

It was read as "the relaxation ladder fired." It is not. `_flag_starvation`
sets the same flag whenever any train's `total_hold_s` exceeds
`GLOBAL_STARVATION_THRESHOLD_S`, on a first-pass OPTIMAL solve that relaxed
nothing. On scenario10 tick 0 under production settings the flag is True with
`worst_hold_s = 14722` and `starved = 12280,12626,40201,40208`, and the ladder
did not run.

Two discriminators, both needed:

  - is `counts["starved"]` present? -> starvation, not relaxation
  - does wall time match the `tier_log` sum? -> one `build_and_solve`, no ladder

Measured: `single descent 1.57 s` against a `tier_log` summing to 1,556 ms.
Anything materially above the tier sum means extra `build_and_solve` calls, and
`solution.solve_count` will not show them -- `solve_with_policy` returns the
LAST rung's solution, so its counters describe that rung only.

Consequence for the cards: the flag lives on `GlobalSolution` and
`_scenario_from` copies it onto every scenario for every resource. Six cards
render "Capacity exceeded" when four trains are starved, including cards whose
own members are not among them.

## Production timing is not the day-11 bench, and tick 0 is not the worst case

Two figures were in circulation, neither describing what ships:

  - `~7 s` per descent, from `test_descent.py`
  - `500-1300 ms` per tier, from a `scope_window()` sizing sweep

`scope_window` is a sizing instrument. `optimize_global` builds payloads from
`candidates`, and that model is about a third the size: 6 resources, 24
intervals, 106 booleans, tiers at 190-310 ms.

Measured in-container, scenario10, `ENGINE=global`, 157 solves:

    min 0.20  median 0.77  mean 1.04  p90 2.19  p95 2.61  p99 3.19  max 5.09

12% exceed 2.0 s; one exceeds 4.0 s. Both the 5.09 s and 3.50 s solves occur at
SEVEN conflicts. The host probe measured 2.60-2.68 s at six. Cost tracks
conflict count, so a tick-0 measurement is a floor, not a ceiling, and any gate
stated as a single-call ceiling is measuring the wrong quantity.

One evaluate is TWO lexicographic descents -- the plan, then the counterfactual
under `forbid=headline`. The counterfactual is ~40% of the cost (2.63 s total
against 1.57 s for the descent alone) and serves only OPT-2. The harness never
approves OPT-2, so it should run `max_scenarios=1` -- and must record that in
`ab-enumerate.env.txt`, because its timing column then describes a
configuration that does not ship.

## T- is not a liveness signal

In a 157-solve container run, six conflicts republished four times each with
byte-identical `predicted_time_to_conflict_seconds` (T-285s, T-233s, T-360s,
T-243s, T-234s, T-213s) -- four `ALERT_COOLDOWN_S` cycles, ~20 sim-minutes of
no movement. Under those frozen cards the plan moved: 12280 held 126 -> 129 ->
157 min, 40201 174 -> 176 -> 204 min, 12626 stand 12 -> 118 min. Severity, T-
and `plan_state` all unchanged.

Do not use a stable T- to conclude the fleet is stable or that the plan is
stable. Whether the cause is the 5 km/h projection floor
(`MIN_PROJECTION_SPEED_KMH`, detector.py:43, applied at :85) or something else
is open; the diagnostic is in `tests/time_evaluate.py`. When reading it, note
that `contested_at = max(window_a.t_in, window_b.t_in)` (detector.py:615) and a
group takes the MIN across constituent pairs (:713) -- so the train that
governs a reported T- is the one arriving LATEST within the binding pair, not
the one nearest the resource. A diagnostic anchored on the nearest train
compares against a train unrelated to the number being explained.