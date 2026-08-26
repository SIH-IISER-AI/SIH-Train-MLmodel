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

## Decision 4 — the anti-starvation constraint  (CLOSED, day 9)

### What it sums

    slack[t,k]      = entry[t,k] - ready[t,k]
    ready[t,k]      = entry[t,k-1] + travel_s[t,k-1] + stop_extra_s[t,k-1]·stopped[t,k-1]
    ready[t,0]      = earliest_arrival_s[t,0]
    total_hold[t]   = SUM over k of slack[t,k]

In words: the time the model chose to leave train t standing, counted once at
the resource where the stand was imposed.

`wait[t,k] = entry[t,k] - earliest_arrival_s[t,k]` is retained separately as
CUMULATIVE lateness. It is what the controller-facing delay figures report. It
is deliberately not what `total_hold` sums: summing lateness would charge one
upstream hold again at every resource downstream of it, and `total_hold` would
grow with route length rather than with standing time.

Asserted in tests/test_global_encoding.py ("total_hold[t] is the sum of its
slacks", every train, both scenarios) and in tests/test_chaining.py case (d).

### Why there is no `forced` term

The day-5 spec had `sum(delay[t,k] - forced[t,k])`. That cannot be encoded.
`forced_s` is only a constant once the order is fixed; `_solve_order` computes
it per permutation, and `optimize_precedence` sets `cap = horizon` in the
unpinned case for exactly this reason. With precedence as a decision variable
there is no per-solve baseline to subtract.

The consequence is that `slack` includes standing because the section ahead is
genuinely occupied. That is intended. Under the per-conflict engine that time
was `forced` and therefore free, which is how 40201 accumulated 8,533 s across
~35 individually-compliant approvals.

### Why the constraint is soft

A hard ceiling on this quantity is infeasible on the production scenario:
12280 carries 8,813 s of queueing on one conflict at tick 0. An infeasible
model gives the controller nothing at all, which is a worse answer to a
CRITICAL alert than a plan that breaks a guideline and says so.

`worst_hold = max over t of total_hold[t]` therefore enters the lexicographic
descent as its lowest tier. The model minimises the worst standing time on any
train, subject to every priority-class cost already fixed.

This tier is load-bearing and was measured: with the descent starved to one
tier of six, `worst_hold` never executed and 12280 carried 22,707 s. With all
six tiers completing, the optimum is 14,722 s — a 35% reduction produced by
the tier alone. See "Solve budget" below.

### The reporting threshold

    GLOBAL_STARVATION_THRESHOLD_S = 7200

A plan holding any train longer than this sets `policy_exceeded` on every card
it produced, and names the train in `counts["starved"]`.

The number comes from operating practice — two hours is where a detention
stops being regulation and becomes a reportable event — and NOT from any
solve. This is the point of the decision. Calibrating the ceiling from the
model's own optimum reproduces the defect this constraint exists to catch:
`cap_s[i] = forced_s[i] + max_hold_s` derived its ceiling from the quantity it
constrained, so no approval could ever violate it. A threshold that cannot
bind is not a threshold.

On scenario10 tick 0 the flag fires: 12280 is held 14,722 s against a 7,200 s
guideline. The card says so.

### The hard bound, and where it is still used

`GLOBAL_HOLD_CAP_MULTIPLIER` (default 0, disabled) applies
`total_hold[t] <= multiplier · max_hold_seconds` as a real constraint, with the
optimize_precedence relaxation on infeasibility.

It is off in production for two reasons: it can return nothing, and when it
binds it forces a second full descent — ~7 s becomes ~14 s, doubled again by
the counterfactual.

It remains available because tests/test_global_hold.py needs a model that
refuses. That test is the day-14 argument in one file: two holds that
`optimize_precedence` approves with `policy_exceeded=False` on both, composing
to 2,448 s on one train, which the global model at `hold_bound=900` declares
INFEASIBLE. Never applied under `pin_order` — the pin exists to reproduce
`_solve_order`, which has no cumulative constraint to reproduce.

### Solve budget (day 11)

    GLOBAL_TIER_BUDGET_S = 2.0     wall clock, PER TIER
    GLOBAL_DET_BUDGET    = 8.0     deterministic time per solve

Per tier, not per descent. Every `Solve()` call re-runs full presolve on the
whole model — CP-SAT carries no state between invocations, and solution
hinting does not change that — so dividing one total across six tiers puts
each below the fixed setup cost. Measured: at 0.15 s per tier the descent
completed 1 of 6 at every total from 0.5 s to 2.0 s.

Observed per-tier cost is 500–1300 ms and roughly flat across tiers. 2.0 s is
~1.5x the slowest observed tier. At 1.5 s the descent completes 6/6 with every
tier OPTIMAL and an identical plan across three runs; below 1.2 s it degrades
non-deterministically — same budget, different tier counts, different orders.

`counts["tier_log"]` records each tier's status and elapsed time.
`counts["truncated"]` is 1 if any tier returned FEASIBLE rather than OPTIMAL,
or if the descent stopped early. A truncated descent is not the lexicographic
optimum and the unpinned order comparison against enumerate's OPT-1 is not
valid against one.

End-to-end: ~7 s for OPT-1 plus the counterfactual, carrying all nine trains
contending SEC-PWL-KSV. Against `ENUMERATION_BUDGET_S = 5.0`, inside which
enumerate explores roughly a third of 5,040 permutations of seven trains and
returns a different OPT-1 depending on machine load.

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

Constants come from `optimizer._prepare()`. This reverses the day-5 statement
that they come from `detector.project()`, and the reversal is the point.

The two agree. `_prepare` originally computed `earliest_arrival_s` as one
accelerating integral over the whole approach at the DESTINATION block's line
speed, ignoring every intermediate restriction on a line running
60/110/100/130/100/130/75/130/60 km/h. Drift against `project()` reached 207 s
and inverted the 12050/12138 arrival order — an input to `forced_s` and hence
to the anti-starvation baseline. Fixed on day 5 by passing `window.t_in`
through. `tests/count_intervals.py` asserts parity; it currently reads 0 s.

Because they agree, `_prepare().earliest_arrival_s` IS the projection's `t_in`,
and the global model can take every constant from `_prepare` — which is what
keeps the isolated encoding gate an EXACT comparison against `_solve_order`.
Taking constants from `project()` instead would make the two engines
incomparable and remove the only measurement that says the encoding is right.

One constant does NOT agree, and it matters. `occupancy_running_s` is the full
block sweep from the train's CURRENT speed. For the resource a train is already
inside, the projection's remaining run is a partial traversal: measured at
scenario10 tick 0, `occupancy_running_s` exceeds it by 278 s on
40201/BLK-114D, 250 s on 40208/BLK-128U and 223 s on 54402/BLK-145U. It is safe
for NoOverlap, where a longer occupancy is conservative, and wrong for chaining.
Chaining therefore uses `earliest_arrival_s` DELTAS, never `exit[k]`.
See the module docstring in `optimizer_global.py`.

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

## Superseded plan items (day 8-9)

Two items from the day-8 plan were not implemented, deliberately. Both would
have been wrong. Recorded here because both look like omissions.

### 8.1 — join scope_window() into the model to supply route order

NOT DONE, and not needed. `_prepare().earliest_arrival_s` IS the projection's
t_in for that resource (the day-5 parity fix passes `window.t_in` through, and
tests/count_intervals.py asserts the two agree to 0 s). A route is traversed in
time order, so sorting a train's intervals by `earliest_arrival_s` recovers
route order exactly; ties break on `resource_id` for determinism. `seq` is
information the model already has under another name.

The transit between two of a train's resources is then
`earliest_arrival_s[k+1] - earliest_arrival_s[k]`, correct whether the two are
adjacent or ten blocks apart — which matters, because payloads contain only
CONTESTED resources, so consecutive model intervals are usually not adjacent on
the route. A `seq`-based chain has no answer for that case.

`scope_window()` remains the sizing and contiguity instrument used by
tests/count_intervals.py. It is not on the model's path. Do not join it.

### 9.4 — the joint gate must be clean

REDEFINED. Before chaining, the joint model was the UNION of independent
models, so an entry-time mismatch against enumerate was unambiguously an
encoding error. After chaining it is not: a train held at resource k arrives at
k+1 later BY DESIGN, and that displacement propagates to whatever follows it at
k+1. Comparing entry times against a per-conflict engine that cannot see the
hold would fail the gate for exactly the behaviour the gate exists to produce.

The gate is therefore split:

  ISOLATED   the encoding verdict. Exact comparison against _solve_order on
             every field, one model per conflict, order pinned. Unchanged and
             non-negotiable: 15/15 conflicts on scenario10, 2/2 on scenario.
  JOINT      structural assertions plus a coordination report. Chain identity
             at every link, one berth per loop per train, total_hold as the sum
             of slacks, one stand per train on the unpinned solve. Entry-time
             deltas versus enumerate are PRINTED, never asserted.

A consequence worth keeping: under the pinned per-conflict orders, 40201 must
be brought to a stand at both SEC-PWL-KSV and BLK-115D. The simulator carries
one hold flag per train, so a two-stop schedule cannot be emitted as directives
at all. The joint gate prints this as a FINDING. Enumerate's composed plan is
not executable, and that argument needs no delay statistic.