# Global CP-SAT model — design decisions

Written on day 5, before any model code. Days 6-14 depend on these five
decisions; changing one after day 8 means re-deriving everything downstream.

Baseline being replaced: `docs/baselines/ab-enumerate.csv`, tag
`day4-harness-parity`. Read `optimizer.py` first — this model reuses its
physics and its objective, and changes only where precedence lives.

## Why

Two measured failures of the per-conflict architecture, both structural:

1. `MAX_TRAINS_ENUMERATED = 5` drops 4 of the 9 trains contending
   SEC-PWL-KSV, and the dropped four are exactly the freights and expresses
   that would be given way. The cap cannot be raised: cap 8 is 40,320
   permutations and 113 s.
2. The anti-starvation cap is scoped per solve (`cap_s[i] = forced_s[i] +
   max_hold_s`). 40201 accumulated 8,533 s across ~35 individually-compliant
   approvals with `policy_exceeded = 0` throughout. No per-conflict solver
   can express the constraint that was violated.

Neither is a tuning problem. Both follow from precedence living in a Python
loop outside the solver.

## Decision 1 — `precedes[i,j,r]` is a retained keyed dict

One `BoolVar` per **ordered** pair per contested resource, keyed
`(train_i, train_j, resource_id)`, held on the solution object after solve.

Ordered, not unordered, and tied by `precedes[i,j,r] + precedes[j,i,r] == 1`.
CP-SAT presolves the redundant half away, so the cost is declaration only.
The reason is day 10: the counterfactual is built by flipping one **named**
boolean and re-solving, and "reverse this decision" has to resolve to a single
identifiable variable, not to an unordered pair whose orientation is implicit.

Linked to the intervals by half-reified constraints:

    entry[j,r] >= exit[i,r] + headway   OnlyEnforceIf  precedes[i,j,r]

`AddNoOverlap` per resource is kept alongside. It is logically redundant given
the pair constraints but gives CP-SAT the disjunctive propagator, and it is
the constraint you point at when a judge asks what prevents a collision.

Store on each solution: `headline = (train_i, train_j, resource_id)` for the
contested resource with the largest contention. That triple is what day 10
flips.

## Decision 2 — `stopped[t,k]` means brought to a stand at the ENTRY to k

Not "stopped somewhere inside k", not "stopped at the exit". The entry
station of resource k is where a loop exists, where a controller can name a
holding point, and where `topology.loop_at()` resolves. Any other reading
makes the directives unmappable to a station code.

Consequence for chaining (days 8-9): `stopped[t,k] = 1` must raise
`entry[t,k+1]` by exactly `travel_from_stop_s - travel_running_s` for the run
between k and k+1 — the deceleration and re-acceleration the train pays for
having stood. Three-case test required before chaining is considered done:
no stop; one forced stop; and an assertion that the raise is exactly that
difference, not approximately.

`stopped[t,k]` decomposes into `in_loop[t,k] + on_main[t,k]`, as in
`_solve_order`. Main-line stands stay modelled — forbidding them makes a
conflict infeasible whenever two trains want one loop, and "no advice" is a
worse answer to a CRITICAL alert than "least-bad".

## Decision 3 — conflict attribution survives the collapse

The global model produces one plan. The UI renders one card per conflict, and
the controller selects OPT-1 or OPT-2 per card. That human-authority
selection is the demo, and it is not negotiable.

So every emitted directive carries `motivating_resource_id`: the contested
resource whose precedence decision caused it. Attribution rule, decided now:
a directive is attributed to the resource carrying the `precedes[i,j,r]`
that binds at the solution — i.e. the resource where this train's ordering
constraint is tight. Where several bind, the one with the largest contention
wins; ties break on `resource_id` sorted, for determinism.

Card decomposition is then a `groupby` on that field, and the existing
`conflict_id_for()` keying still works. Retrofitting this on day 11 means
re-deriving attribution from solved times, which is guesswork.

## Decision 4 — `total_hold[t]`, the constraint enumerate cannot state

New, and the direct consequence of the 40201 finding.

    total_hold[t] = SUM over k of (delay[t,k] - forced[t,k])
    total_hold[t] <= max_hold_s

One expression per train over the whole window, constraining **cumulative
discretionary** delay. `DEFAULT_MAX_HOLD_SECONDS = 900` unchanged; what
changes is the scope it applies to.

This is the row that makes the day-14 merge gate meaningful: enumerate cannot
express it, so `max_cumulative_hold_s` is a column where the global engine
should win outright rather than trade.

OPEN — must be resolved before this is encoded: 40201's 8,533 s has not been
decomposed into stands versus regulated crawling. If most of it is REGULATE
slow-running rather than holds, then `total_hold[t]` constrains the wrong
quantity and the expression must sum total discretionary delay, not hold
time. Run the arm A / arm B per-train comparison and settle it first.

Relaxation mirrors `optimize_precedence`: if infeasible under the cap, re-solve
at 3x and set `policy_exceeded`. A plan that breaks a guideline beats no plan,
provided it says so.

## Decision 5 — the simulator's refusal rules become model constraints

Measured at tick 0: 1 refusal in 13 directives. The model must not emit plans
the injector will drop.

* **No same-direction `STAND_ON_MAIN`.** An overtake needs a loop; standing on
  the main deadlocks the train being waited for. Encode as: if
  `direction[i] == direction[j]` and `precedes[i,j,r]`, then
  `on_main[j,r] == 0`.
* **Loop fit and identity** via `topology.loop_at(entry_station_id,
  train_length_m)`. A loop with no id cannot be berthed against under the
  capacity constraint — treat as unavailable rather than let two trains share
  an anonymous slot.
* **Loop capacity per `loop_id` across the whole window**, as `NoOverlap` over
  optional intervals. This is the case per-conflict solving cannot see: today
  loop `NoOverlap` holds within one conflict, and two independently-solved
  conflicts can book the same loop. Measured `contested_loops = 0` at tick 0,
  but that is one tick and the audit flags only same-tick collisions.
* **A hold station the train has already passed** is refused by the injector.
  The window scoping already excludes it: an interval only exists for a
  resource the train has yet to enter.

## Physics source of truth

Constants come from `detector.project()`, not from `optimizer._prepare()`.
`_prepare` is single-resource by construction — it computes
`earliest_arrival_s` as an unimpeded run from the train's current position to
one block, at that block's line speed, ignoring every intermediate speed
restriction. Measured drift against `project()` was up to 207 s and flipped
the 12050/12138 arrival order. Fixed on day 5 by passing `window.t_in`
through; `tests/count_intervals.py` asserts the two agree.

## Window scope

`(train, resource)` pairs with projected `t_in` inside `GLOBAL_HORIZON_S`,
default 1800 s to match the detector's own horizon. A wider window solves for
contention the controller has not been warned about; a narrower one leaves a
raised alert unaddressed.

Measured on scenario10 at tick 0:

| horizon | intervals | resources | contested | precedes | largest |
|--------:|----------:|----------:|----------:|---------:|--------:|
|    900s |        58 |        24 |        17 |      108 |       4 |
|   1800s |        74 |        28 |        17 |      214 |       9 |
|   2700s |        94 |        40 |        22 |      236 |       9 |
|   3600s |       136 |        55 |        34 |      332 |       9 |

Gate is 300 intervals. 1800 s passes at 74. Going to 2700 s buys 5 more
contested resources, all 2-train double-line blocks — cost without decisions.

## Reversibility

`optimizer.py` is never deleted. `ENGINE=global|enumerate` selects, default
`enumerate`. If the day-14 merge gate fails, the branch does not merge and
becomes the roadmap slide with measured numbers.

The switch sits ABOVE `evaluate()`'s conflict loop, not at the
`optimize_precedence` call site: the global model solves once per evaluate for
all conflicts, then decomposes per decision 3. A drop-in at the call site is
not possible.