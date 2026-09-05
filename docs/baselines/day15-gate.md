# Day-15 pre-registration and freeze

Written at the end of day 14. Section 1 rows are unknown at the time of
writing; section 2 rows are measured true and carry no evidential weight;
section 3 is reported without a threshold.

Section 4 records amendments, including one hard-fail row that was written,
run, and **failed**. That row was not rewritten to pass. The replacement is
pre-registered before the run that tests it.

Configuration under test: `ENGINE=global`, `APPROVAL_RULE=conflict_id`,
`GLOBAL_HOLD_TIER=worst_hold`, `REGULATION_FLOOR_FRACTION=0.35`, n=15,
arm A, 1080 ticks, `data/scenario10.json`.

---

## 1. Hard-fail rows

### 1.1 Throughput — both conditions must hold

**(a)** Clearances through `SEC-PWL-KSV` per sim-hour at n=15 must be no
worse than arm B's flat 1.333 on **at least 12 of 15 seeds**.

**(b)** The paired difference against arm B on the same data must have a
95% CI **lower bound above −0.333**.

The margin in (b) is one clearance over the three-hour run — the granularity
of the measure itself. It is not read off the data.

Last observed with the floor: 12/15 on (a), CI [−0.092, +0.226] on (b).
Both pass. (a) alone passed at exactly the threshold with zero margin; the
seed-count form discretises a continuous measure and lands knife-edge on
data showing a clean +0.178 at t = +2.26. Adding (b) as a conjunction cannot
move the goalposts in the engine's favour and removes the coin flip from the
metric in the problem statement.

Discrimination check: floor 0.35 passes both. Unfloored baseline fails both
(8/15; CI lower bound −0.369). Floor 0.20 fails both (10/15; −0.412).

### 1.2 Floor must not bind inside the regulating range — REPLACEMENT ROW

`REGULATION_FLOOR_FRACTION=0.50` at n=15. Throughput must **not** improve
over 0.35, and `tests/test_regulation_floor.py` assertion 3 must fail at
0.50, demonstrating that a floor above `MIN_REGULATION_FRACTION` binds on
waits the model has ruled absorbable.

Not run. This is what distinguishes "the floor is where the two functions
agree" from "the floor is a knob that was turned until throughput improved".
If throughput improves at 0.50, the floor is a tuned parameter and must be
presented as one.

See section 4.1 for the row this replaces and why.

### 1.3 Lag stability in-container

Over a full run in the container stack, the solver backlog must be **no
larger at the end than at the start**, with p95 `evaluate` under two tick
periods, both reported alongside.

Last measured in production on day 12: median 0.77 s, p95 2.61 s, max 5.09 s
against a 2.0 s tick. Never measured as a backlog trend, and never with the
floor applied. A single-call ceiling is the wrong form — `DECISIONS.md`
already says so.

### 1.4 Replay coverage across seeds

Every train the model priced a hold for must receive a directive: **0 misses
across 15 seeds**.

`tests/test_directive_replay.py` currently checks this at tick 1 on one
scenario, not across seeds.

### 1.5 Ordered-train delta positivity across seeds

For every train marked `<- ordered` in the replay table, observed standing
minus priced hold must be **≥ 0 at n=15**.

Held at tick 1 under `worst_hold`, `sum_hold` and the floor. Not verified
across seeds. Day 13 saw 12138 read −5,119 on the cumulative-hold-versus-
single-directive mismatch, and the floor moved 12626 from +921 to −1,669 at
tick 1, so the failure mode is live even though neither train is `<- ordered`.

### 1.6 Card/plan agreement

**Zero** approvals may produce a non-empty clause list with an empty
directive set.

Roughly a third of day-12 approvals did exactly this. Unfixed at time of
writing.

### 1.7 Hold discharge under the floor

Every `HOLD_AT_LOOP` accepted during a full run must either discharge or be
still within its `hold_expires_sim_s` at end of run: **0 latched holds past
their timeout, across 15 seeds**.

The floor changed discharge behaviour at tick 1 — 12280 from cleared at
10,440 s to never, 40208 from 17,710 s to never. `HARNESS-NOTES` records the
post-condition as unasserted with three predicates that misreport
correctly-expiring holds, so the predicate must be written before this row
can be evaluated.

---

## 2. Regression guards — known true, not evidence

Measured true today. Listed so a regression is caught. Citing any of them as
support for the engine would be the error this document exists to avoid.

* **Emitter/physics saturation** (`tests/test_regulation_floor.py`).
  Verified to discriminate: **exit 1 with 13 saturation failures on
  unfloored code** including the observed seed-3 case at 6.333 km/h against
  a 33.250 floor; **exit 0** at `REGULATION_FLOOR_FRACTION=0.35`. Assertion 1
  (boundary identity) is algebraic and passes unfloored — it is a canary,
  not a gate. See section 4.2.
* The floor changes emission only, not the plan: identical holds, precedence,
  headline and `worst_hold = 14722 s` at floor 0 and 0.35 in
  `tests/test_descent.py`
* `refused_directives_total = 0` at n=15
* `contradictory_instructions_total = 0` at n=15
* Determinism above cap 6 — byte-identical holds, precedence and headline
  across repeated runs
* All 6 lexicographic tiers OPTIMAL, `truncated = 0`
* Directive sets executable: one stand per train per window
* Enumerate's composed plan remains inexpressible (40201 must stand twice)
* Cap-5 truncation still drops exactly the trains that would be given way
* `run_all.sh`: 0 fix checks broken, 11 tests, 0 failures at the shipped
  configuration

* a note that refused_directives_total = 0 was cross-checked on one sample with a positive control and held (6 emitted, 6 issued), that the check is n=1, and that re-targeting is not counted as a refusal by design.a note that refused_directives_total = 0 was cross-checked on one sample with a positive control and held (6 emitted, 6 issued), that the check is n=1, and that re-targeting is not counted as a refusal by design.

---

## 3. Report and note — no threshold

* Premier and fleet deltas with 95% CIs and an **explicit underpowered
  statement**. Premier SD is 5,120 s against a 15-seed ceiling; the design
  cannot resolve these and must not be quoted as if it could.
* `standing_s_total` and `regulated_s_total`, with the stated limitation
  that the latter measures duration under a binding regulation, not
  severity, and could not have shown the floor's effect.
* Stand-impossible degradation count per run — 611 unfloored, 299 floored.
* Starvation counts and named starved trains.
* **New instrumentation:** `directives_per_evaluate` and
  `distinct_trains_covered_per_evaluate`, both engines. This is the
  measurement that tests whether the directives-per-approval gap (0.95
  against 2.10) is real or an artefact of the counting unit. Reporting only;
  no threshold, and no code outside the harness moves on it.
* The 47% of the throughput gap unexplained after six ablations, named as
  unexplained.

* the F5 per-train table (12001 at 10,790 s of 10,800, never lifted, 
  but cleared the section post-floor — severity bounded by the floor);
  the 1.5 calibration finding at n=19; and the re-target count, 
  19 events across 30 samples, 17 of them at MTJ.
---

## 4. Amendments

### 4.1 Row 1.2 (original) — FAILED, not rewritten

**As written on day 14, before the run:** "`REGULATION_FLOOR_FRACTION=0.20`
at n=15 must also improve throughput against the unfloored baseline — sign
only, no significance threshold."

**Result:** d = +0.000, t = 0.00, 2 better / 2 worse / 11 tied. Mean 1.2222,
identical to the unfloored baseline to four decimals. **The row failed.**

The row was mis-specified rather than unlucky: it tested sensitivity *below*
`MIN_REGULATION_FRACTION`, where the parameter is not a continuum. There is
exactly one value at which `regulated_speed_kmh` and `absorbable_delay_s`
agree, and a floor below it restores no consistency. The meaningful
sensitivity question is above the boundary, which is row 1.2's replacement.

The replacement is a **post-hoc amendment** and does not carry the
evidential weight of a passed pre-registration. The floor's provenance must
therefore be argued on separate grounds, labelled as weaker: 0.35 is
`MIN_REGULATION_FRACTION`, present in the codebase before day 14 and not
chosen after seeing any result, and the algebra makes it the unique
consistent value.

### 4.2 Row 1.3 (original) — demoted to a regression guard

**As written:** a unit test asserting
`regulated_speed_kmh(d, v, absorbable_delay_s(d, v, f)) == v·f`, listed as a
hard-fail row and described as "the gate on the day-14 change".

It is not. That identity is algebraic — at
`wait == (d/v)(1/f − 1)`, `base + wait = (d/v)(1/f)`, so the quotient is
`f·v` identically. It passes on unfloored code, passes if
`REGULATION_FLOOR_FRACTION` is deleted, and passes on the seed-3 run that
produced 6.333 km/h. A section-1 label on a section-2 fact, one level down
from the error this document was built to avoid.

What guards the floor is behaviour **past** the boundary, which is where
`emit_directives` lives: it passes the full slack, and slack exceeding
absorbable is exactly the condition under which the model set `stopped = 1`.
The test now carries three assertions plus the observed seed-3 case;
assertion 2 (saturation) is the one that can go red, and it did. Moved to
section 2 with its discrimination evidence, since it is now measured true.

### 4.3 Row 1.1 — second condition added

The seed-count form alone landed at 12/15 against a 12/15 threshold. The
paired-difference condition (b) was added as a conjunction, with its margin
fixed from the measurement granularity before the day-15 run. Adding a
condition to a conjunction cannot weaken the gate.


4.4 — row 1.4 FAILED. Now with the population number: 30 samples, 246 priced holds, 42 misses. 6 at t1 (4.8%), 36 at t540 (30%). All 15 seeds miss at t540. Cause established by reason-code trace, 1:1 with the gate, zero residual: unreachable stands at distance_m = 0.0. Local degradation falsified — 0 useful directives of 23. Repair upstream in optimiser_inputs, behind the freeze. Branch (c).
4.5 — row 1.3 FAILED, instrument counts evaluates not ticks. Eleven discrete events, nine in the first 70 ticks, remaining 2,029 gaps net −3.2 s.
4.6 — row 1.5 FAILED as written, not rewritten, plus the discriminating-power finding: timeout arm n=15 cannot fail (min delta +1,982 s, structural), leader_passed arm n=4 cannot pass, 20172 passed at 2% of priced. Replacement pre-registered in Advisor 2's event-log form, not mine — mine inherited the unfailability.
4.7 — fourth limb, with a named day, not "scheduled".

---

## 5. Decision rule

**If 1.1 fails:** `ENGINE` default stays `enumerate` for the demo, and global
becomes the roadmap slide with measured numbers. `GLOBAL_MODEL_SPEC.md`
already commits to this. Honouring the pre-registration on stage is worth
more than the engine.

**If 1.2 fails** (throughput improves at 0.50): the floor is a tuned
parameter. It may still ship, but the addendum and the deck must say so, and
the consistency argument is withdrawn.

**What reverting costs, stated so the choice is made with both sides
visible.** Enumerate carries 12.3 refusals and 3.7 contradictions per run, is
nondeterministic above cap 6, and drops 4 of 9 trains at the headline
conflict. Those are visible on screen during a demo. Throughput is a number
on a slide we control. That asymmetry is why the regression was attacked for
a full day rather than routed around — it is not a reason to fail the gate
and ship anyway.

**Not a criterion:** that global is *no longer significantly worse* than
enumerate on throughput, fleet and premier. At n=15 with these SDs the
design is underpowered for equivalence claims. Absence of a significant
difference is not parity, and this document does not claim it is.

---

## 6. Freeze

On completion of section 1, freeze as `DECISIONS.md` defines it and tag
`day15-freeze`. Frontend and simulator remain outside the freeze; WS-C card
correctness proceeds on days 15–16, including the `_scenario_from`
two-predicate split, which is scoped to the defect it actually causes — a
card naming a train, a loop and a duration with an empty directive set — and
not to the throughput gap.

4.4 Row 1.5 — FAILED, cause established, repair outside the day-16 window

9 misses across seeds 1–3 at ticks 1 and 540. A per-train reason-code trace in emit_directives, validated 1:1 against the replay gate with zero residual, attributes every miss to one branch: a train whose only positively-priced slack sits at a resource it has already reached, so distance_m clamps to 0 and the stand is unreachable. All 23 traced instances read distance_m = 0.0.

Degrading these to a regulation, the fix the adjacent stand_impossible branch uses, produces zero useful directives: regulated_speed_kmh returns at its distance_m <= 0 guard, giving 0 km/h on the 15 stopped cases — which, absent a REGULATE expiry (F5), pins the train for the remainder of the run — and the train's current speed on the other 8. The silent drop is the safer behaviour.

The repair is upstream, in the model's decision to price stopped = 1 for a train at distance_to_bottleneck = 0, which is detector.optimiser_inputs() and is behind the freeze. Recorded as failed. Roadmap.

The row's threshold detected a real defect and is retained. Its implicit remedy is withdrawn: requiring a directive per priced hold would make the railway worse in 23 of 23 observed cases. The row is restated post-freeze as a reported count of unreachable_stands, which optimize_global already maintains.

4.5 Row 1.4 — FAILED, instrument measures a different quantity than the row states

The row requires the solver backlog to be no larger at the end than the start. The instrument cannot measure that. main.py:513 sets saw_tick as a boolean per xread batch and main.py:574 reads it once, so evaluate() runs at most once per batch however many SIMULATION_TICK events it contained; _lag_ticks therefore counts evaluates, not ticks, and drift_s = wall − _lag_ticks × TICK_SECONDS accrues 2.0 s per coalesced tick. A backlog in this loop manifests as skipped evaluates, which is the term drift is measured against.

Measured on the 2,040-tick trace: 10 inter-tick gaps exceed 3.0 s and carry 16.3 s of excess against 13.384 s of total drift; the remaining 2,029 gaps net negative. The failure is ten discrete events, not a trend. The eighths table in F16 read structure out of ten events across eight buckets.

Recorded as failed. The row is not amended, and no lag figure from this instrument is quoted.

4.4 Row 1.4 (replay coverage) — FAILED, cause established, repair behind the freeze

30 samples, seeds 1–15 at ticks 1 and 540. 246 priced holds, 42 without a directive. 6 at t1 (4.8%), 36 at t540 (30.0%). Every one of the 15 seeds misses at t540, between 1 and 3 each, against a threshold of zero.

Cause established by per-train reason codes added to emit_directives on day 16 and proven measurement-only (seed 7 and seed 3, full runs, 0 mismatches on 25 and 26 columns). The trace matches this gate 1:1 with zero residual — same count, same train list. One mechanism: a train whose only positively-priced slack sits at a resource it has already reached, so distance_m clamps to 0.0 and the stand is unreachable. All 23 traced instances read exactly 0.0.

The local repair is falsified, not merely unattractive. Degrading these to a regulation, as the adjacent stand_impossible branch does, yields 0 useful directives of 23: regulated_speed_kmh returns at its distance_m <= 0 guard, producing 0 km/h on the 15 stopped cases — which, absent a REGULATE expiry, pins the train for the remainder of the run — and current speed on the other 8. Both options at that point in the code are wrong; the silent drop is the less bad of two bad options, and both are symptoms of a defect upstream.

The repair is in the model's decision to price stopped = 1 for a train at distance_to_bottleneck = 0, in detector.optimiser_inputs(), which is frozen. Recorded as failed, scheduled day 20.

The row's threshold detected a real defect and is retained. Its implicit remedy is withdrawn: requiring a directive per priced hold makes the railway worse in 23 of 23 observed cases. Restated post-freeze as a reported count of unreachable_stands, which the solver already maintains and does not export.

4.5 Row 1.3 (lag stability) — FAILED; the instrument measures a different quantity than the row states

The row requires the solver backlog to be no larger at the end than the start. The instrument cannot measure that. main.py:513 sets saw_tick as a boolean per xread batch and main.py:574 reads it once, so evaluate() runs at most once per batch however many SIMULATION_TICK events it contained. _lag_ticks counts evaluates, not ticks, and drift_s = wall − _lag_ticks × TICK_SECONDS accrues 2.0 s per coalesced tick. A backlog in this loop manifests as skipped evaluates, which is the term drift is measured against.

Measured on the 2,040-tick in-container trace: eleven inter-tick gaps exceed 3.0 s, carrying ~17 s of excess against 13.384 s of total drift; the remaining 2,029 gaps net −3.2 s. Nine of eleven fall inside the first 70 ticks. Excess splits 11.0 s not spent evaluating against ~6.2 s of evaluate overrun. Eleven discrete warmup events, not a trend. The day-15 eighths analysis read structure out of eleven events across eight buckets.

Recorded as failed. The row is not amended and no lag figure from this instrument is quoted. What the instrument does support and what will be stated instead: post-warmup evaluate p95 0.898 s against a 2.0 s tick period, in-container over 2,040 ticks. That is a solve-cost claim, not a backlog claim. Naming the broken instrument alongside the sound one is stronger than declining to answer.

4.6 Row 1.5 (ordered-train delta) — FAILED as written, not rewritten; and the row has no discriminating power

Two failures on seed 3 tick 1: 40201 at −5,767 s, 40208 at −841 s.

The cumulative-hold-versus-single-directive mismatch, proposed as the cause, is real and is now fixed — priced_hold_seconds and priced_resource_id were added to every directive on day 16, permitted by the freeze rule, which allows a directive to take a field but not to change a value. It affects 6 of 44 ordered trains. Neither failing train is among them: 40201 reads slack_s = 11817, total_hold_s = 11817 and 40208 reads 14701, 14701. Substituting the per-resource quantity leaves both deltas unchanged.

SIM_TRACE_HOLDS gives the sequence for both: issued_hold_at_loop → berthed → release_blocked (main_occupied) → discharged_loop (leader_passed), expiry never reached. Execution was correct. What the row caught is that the model overpriced the hold — the directive carries two release conditions and the model prices only the timeout.

Discriminating power, measured across seeds 1–3 at both sample ticks, n=19 holds with a discharge inside the watch:

timeout, n=15: held/priced median 1.58, range 1.19–6.14, minimum delta +1,982 s.
leader_passed, n=4: median 0.27, range 0.02–0.99.

The timeout arm cannot fail. release_timeout_seconds = delay_s + DIRECTIVE_RELEASE_TIMEOUT_S with delay_s ≥ slack_s, so a hold run to timeout exceeds its priced value by at least 1,800 s by construction. The leader_passed arm cannot pass, and is not detected reliably: 20172 discharged at 2% of priced and the gate scored it a pass, because stood counts speed_kmh < 1.0 from any cause across an 18,000 s watch. Identical mechanism to 40201, opposite verdict. Corroborated at n=15: all 9 negatives fall at t1, none at t540, because by t540 the timeout arm dominates.

The verdict is uncorrelated with execution fidelity. The row is recorded FAILED as written. A post-hoc discovery is legitimate when it adds a fact alongside a recorded verdict and illegitimate when it changes one; this adds.

Replacement, pre-registered before the run that tests it. For every hold in the plan, SIM_TRACE_HOLDS must show an issued_* event at the station the model priced, and thereafter either a discharged_* inside the watch or a release_blocked naming an occupancy. A hold with no issued_* event, or issued at a station other than the priced one, fails. Discharge reason and held/priced ratio are reported with no threshold. This can fail on all four documented paths — re-targeting, _next_loop_station returning None, the silent continue, and a hold that neither discharges nor blocks — and does not depend on delay arithmetic. It lands in tests/test_replay_seeded.py, outside the freeze.

Calibration finding, n=19, reported not asserted. The model prices one release condition while the directive carries two, so realised standing overshoots when the timeout fires and undershoots when the leader clears. worst_hold_s_max is therefore neither an upper nor a lower bound on realised standing. A fix would require estimating physical release from the leader's projection, which depends on the leader's plan, which depends on the hold being priced. Endogenous; a structural encoding change; not attempted. Recorded with magnitude.

4.7 Row 1.6 (card/plan agreement) — FAILED; splits into two defects of unequal severity

180 cards across seeds 1–15 at ticks 1 and 540. 83 with an empty directive set. Stable across five runs including two with GLOBAL_TIER_BUDGET_S and GLOBAL_DET_BUDGET lifted.

22 benign. The action is clear without regulation; nothing is asked, so nothing should be attached.
61 actionable — a card naming a train, a station or loop and a duration, which does nothing when approved. 34% of cards, matching the day-12 estimate of roughly a third, now measured across 15 seeds.

The 61 split again by whether the named trains receive a directive elsewhere in the plan:

26 presentation defects. Every named train is covered on another card. The plan is sound; the card misreports it. Repair is in _scenario_from, which this document's freeze rule leaves unfrozen and permits to take additive fields. Scheduled day 17.
35 coverage holes. No directive anywhere in the plan. This is row 1.4's defect at card level. Repair is upstream and frozen. Scheduled day 20 with 4.4.

Four conflict ids recur: CONF-0B11FF25, CONF-A0499651, CONF-D3EE6385, CONF-2A5751EF. Hold Bhopal Shatabdi Up 12001 at LOOP-MTJ-01 at MTJ for 46 min appears on seeds 1, 2, 3, 5 and 6 at t540 under the same id with no directive attached.

Gated by tests/check_card_agreement.py, which reports all four counts.

4.8 Row 1.2 (replacement) — assertion clause VOID, superseded by change C6

The row requires tests/test_regulation_floor.py assertion 3 to fail at REGULATION_FLOOR_FRACTION=0.50, demonstrating that a floor above MIN_REGULATION_FRACTION binds on waits the model has ruled absorbable.

After C6 collapsed the two constants into one parameter, that configuration does not exist. absorbable_delay_s and the saturation floor are computed from the same f, so they agree at every value. Measured: at f=0.50 the express floor is 47.500 km/h with absorbable 568.4 s; at f=0.35, 33.250 km/h and 1,055.6 s. Both moved together. FLOOR-PASS at both, exit 0 at both. Assertion 3 cannot fail at any fraction.

The clause is void — invalidated by a change made after it was written, not passed and not failed. The throughput clause remains live and is evaluated separately.

4.9 Cross-cutting: three hard-fail rows carried assertions that could not go red

4.2 demoted the original row 1.3 because its assertion was an algebraic identity that passed on unfloored code. 4.6 records row 1.5's timeout arm as structurally unfailable by an arithmetic bound. 4.8 records row 1.2's assertion clause as void after the parameter collapse.

Three instances, three distinct mechanisms — an identity, a bound, a superseded configuration. The common failure is that none was checked against a configuration that should make it fail before being written as a hard-fail row. Section 1's discrimination check exists for exactly this and was applied to row 1.1 alone.

Standing requirement, effective now: no row enters section 1 without a stated configuration under which it goes red, and evidence that it does.

And to the section-4 preamble, the fourth limb:

A finding, as distinct from a row, whose cause is established and whose repair is inside tier 0, is recorded with its measured magnitude and its cost-to-repair, is assigned a named day, and the reporting limitation it creates is stated in section 3. "Scheduled" without a day is indistinguishable from deferred.