Day 13 complete. Harness ported, 11.4 fixed and gated, n=15 measured. Summary below; the plan changed in four places.

Sizing killed the overnight run
One seed, 1080 ticks, arm A under global: 31.5 s. Not 35 minutes. n=15 runs in about seven. Your decision rule triggered in the other direction — there is no overnight job, day 14 is analysis only, and I ran the measurement in the foreground.
Two corrections to block 2 as specified. PROBE_TICKS=1080 could not have produced solves-per-tick: boot() runs N ticks and then evaluates once. Cumulative solve count needs the harness loop, so the sizing run and the harness port were the same work — the port had to come first, not after block 3. And a run backgrounded under block 1 would have measured a pre-fix engine, so I scoped it runtime-only and discarded the outputs.
Cost breakdown from the sizing run: solve_s 30 of wall 31.5, solves 855 over 1080 ticks. 95% of runtime is CP-SAT and 225 ticks raise nothing. Ticks 20→60 cost 2 s of solve between them — the first few ticks carry almost all of it, because in gridlock the decisions are forced and the model is cheap.

stand_station_id landed, with a change of shape you should know about
Implemented as specified: hold_station_id untouched so topology.loop_at() is unaffected; new stand_station_id = last station in [train.distance_km, window.start_km], None when there is none; new Topology.station_before(); emission filter only, no model constraint, per the HARNESS-NOTES ruling about model.Add(stopped[key] == 0) and the unexplained 344/245 result.
First attempt failed your own gate silently, and it is worth recording. With the station corrected, 12626's stand had nowhere to land — it sits at 44.53 km, BVH is astern, PWL is beyond the resource. The directive was simply dropped. Directive set went 9 → 8, test_directive_replay went GREEN, and every assertion still passed — because the per-train loop iterates the directive set, so a train losing coverage disappears from the assertions instead of failing one. That is the third instance today of the pattern in HARNESS-NOTES: suite green, engine worse.
Two fixes followed. The None case now degrades to a REGULATE rather than emitting silence — your notes already say "only regulation can shed time" on such an approach, and coverage beats a costed decision the card does not show. And the replay gate gained a coverage check over the set the model priced, not the set it emitted:


ok   every train the model priced a hold for gets a directive: 0 (want 0)

That check would have failed on the 8-directive state. It is the assertion your headline resource carries a directive proposal was reaching for, in a form that cannot fire on a correct solve.

The delta table's legend was wrong, and it made row 3 look failed
Post-fix, 12138 read −5119 (5,779 s priced, 660 s stood). By the row as written that is a fail. Nine identical replay runs confirm the table is deterministic, so it is not noise.
It is an instrument defect. expected_hold is cumulative_hold_seconds = total_hold[t], the sum of standing slack across every resource. emit_directives issues one directive at one motivating resource. A train can carry priced standing and receive a REGULATE — which by design produces slow running, not standing — so its row compares two different quantities.
The discriminator is already in the table. Every train marked <- ordered is positive: 12002 +16305, 12280 +2165, 40201 +251, 40208 +2318. And pre-fix, 12626 was <- ordered with −1669, on a stand genuinely refused at BVH. The negative-delta signal works exactly as designed when restricted to ordered trains, and is meaningless otherwise. Legend corrected; not promoted to a check() yet, per the print-before-assert rule — it is validated on one scenario at one tick. Day 14 tells us whether it holds at n=15, and then it becomes a gate row.
Same two-predicate root as the _scenario_from clause/directive split from day 12. Third symptom of it.

Gate result: pass, rows 3 and 5 amended in writing
Row 1 (9 directives): pass. Row 2 (no INFEASIBLE): pass. Row 4 (BVH refusal to zero, MTJ reported): pass. Row 5 amended — the stand is genuinely impossible on that approach, so the breakdown is 4 HOLD_AT_LOOP / 5 REGULATE / 0 STAND_ON_MAIN, and the row is now "total 9 and no priced train loses coverage." Row 3 re-scoped as above. Both amendments recorded below the pre-registration, not edited into it.

New finding: stand_impossible is systemic, not a 12626 quirk
Across the n=15 the FINDING fires on 12138 at BLK-126U and on 54402 seven times at BLK-127U/128U. stand_station_id returns None routinely on the UP main blocks, so the regulation fallback is load-bearing rather than an edge case, and the engine is systematically substituting regulations for stands wherever a block-level resource has no station between the train and the entry.
Two questions this raises for day 15, and I do not have answers yet: whether a regulation actually sheds what the stand was priced to shed (the model chose STAND because absorbable_s bounded regulation), and whether "no station on the approach" is a topology gap in scenario10 or a real property of block-level resources. The first is measurable off the n=15 delta columns; the second is a data question.

n=15 measured. No detectable delay difference, and the design cannot detect one.
Paired, 15 seeds, no seed_attempts mismatch. Global minus enumerate, arm A:
metricΔ95% CItsignPremier delay+2,304[−532, +5,140]p≈0.1011/15, p≈0.12Fleet delay+3,198[−223, +6,619]p≈0.078/15, p≈1.0Throughput−0——worse on 2/15
Neither is significant. I sent an earlier read of the unpaired means as "global is worse"; that was wrong and I am withdrawing it.
The paired SD is 5,120 against the 2,344 recorded for the enumerate-vs-B comparison. The reason is in the baseline's own limitations: arm A spans 3.5× on Premier while arm B spans 7%. Engine-vs-control pairs high variance against near-constant. Engine-vs-engine pairs two high-variance arms and the variances add. With the seed-redraw ceiling at 15, more runs add runs and not evidence.
So the day-15 gate cannot be "global beats enumerate on delay." It is underpowered by construction and will be on any future day. Pre-registering it would pre-register a coin flip. I want your read before I write the replacement, but my proposal is that the gate rests entirely on the claims that need no delay statistic: determinism above cap 6, executability of the composed plan, contradictory_instructions_total 0/15, cumulative hold inexpressibility, cap-5 truncation at largest_group 9, and refused_directives_total 0/15.
One further result, which I think is the most important thing measured today. ab-enumerate.md diagnosed the +5,746 s fleet regression as the cost of ~35 independent locally-optimal decisions with nothing reconciling them, and predicted a single model would remove it. Global posts contradictory_instructions_total = 0 on all fifteen — the incoherence is gone and the fleet delay did not improve. The diagnosis was wrong, or the incoherence was not the mechanism. That falsifies a claim we wrote down in advance and then tested, which is a stronger thing to present than a delay number we cannot defend.
What changed in the plan
No overnight run. Day 14 is analysis plus the day-15 gate pre-registration, which is still the only item on the critical path without an owner.
Harness port moved ahead of block 2 rather than after block 3, because block 2 depended on it.
The replay gate gained a coverage assertion. Recommend it as a day-15 gate row.
Day 16 gains one item and it is not cosmetic: total_hold vs single-directive is now confirmed as the same defect behind three separate symptoms.
Everything committed on days-8-11-chaining. Gate at docs/gates/day13-stand-station.md, baseline at docs/baselines/ab-global.csv with its env file.

## Addendum (day 14) — correction, six ablations, two failed predictions

Appended below the original day-13 report, not substituted for it, per the
convention used for the withdrawn intra-class-shuffling claim in
`ab-enumerate.md`. The original text stands as written; this section states
what is wrong with it and what replaced it.

Two predictions made inside this addendum during day 14 were themselves
wrong, and one claim was retracted in full. All three are marked WITHDRAWN
below and left in place rather than deleted.

### Correction: "Throughput −0 — worse on 2/15" is wrong

The day-13 report recorded no throughput effect. The committed CSVs say
otherwise. Paired on seed, arm A, `ab-global.csv` minus `ab-enumerate.csv`:

| metric | global | enumerate | arm B | Δ vs enum | t | better/worse/tied |
|---|---|---|---|---|---|---|
| Throughput (clear/sim-hr) | 1.222 | 1.556 | 1.333 | −0.333 | −2.84 | 2 / 9 / 4 |
| Fleet delay (s) | 46,088 | 42,890 | 37,144 | +3,198 | +2.01 | 7 / 8 / 0 |
| Premier delay (s) | 11,741 | 9,437 | 12,352 | +2,304 | +1.74 | 4 / 11 / 0 |

95% CI on throughput is [−0.585, −0.082] and excludes zero. Paired t
p ≈ 0.013. Verified independently against the CSVs by two scripts.

Three consequences the original report did not draw:

1. **The "design cannot detect a difference" conclusion was too broad.** It
   generalised the Premier SD of 5,120 s to the whole apparatus. Throughput
   has SD 0.45 and is fully powered at n=15. The design detected a
   difference. It went against global.

2. **Throughput is the metric in the problem statement.** SIH-47 is
   *Maximizing Section Throughput*. At 1.222 the engine was 21% below
   enumerate and below arm B's flat 1.333 — no better than leaving the
   railway alone.

3. **corr(throughput Δ, fleet Δ) = −0.894.** These were never two findings.
   Both columns were reading one behaviour.

Seed 3 posts 0.000 clearances in three sim-hours under global against 1.333
under enumerate, +7,517 s Premier and +19,537 s fleet. Not noise.

### A1 — the zero is trains held short, not gridlock inside the section

Instrumented `SEC-PWL-KSV` entry/exit transitions on seed 3, both engines.
The counter fires on transition *off* the resource, so a zero was ambiguous
between "entered and never left" and "never arrived".

Under global, **eight of ten trains never entered**. Section geometry is
km 59–97 for DOWN trains and from km 97 for UP:

| train | dir | final km | verdict |
|---|---|---|---|
| 12626, 12002, 12050, 20172 | DOWN | 49.7, 39.5, 46.9, 43.2 | all short of km 59 |
| 12001, 12138, 12280, 54402 | UP | 82.6, 80.9, 76.9, 72.9 | all short of km 97 |
| 40201 | DOWN | 70.6 | inside, never left |
| 40208 | UP | 109.1 | inside, never left |

Under enumerate on the identical seed, 12001, 12280 and 54402 all finish at
96.95 and 12138 at 97.95 — queued at the section entry — while 40201 reaches
148.4 and 40208 reaches 138.0. 12002 completes all 195 km and recycles.

The throughput counter is not undercounting. The plan parks the fleet
upstream of the bottleneck for three sim-hours.

### A4 — the excess is regulated slow-running, not standing

The decomposition open since day 5 (`standing_s` vs `regulated_s` columns).
Paired, n=15, global minus enumerate:

| column | global | enumerate | Δ | t | seeds worse |
|---|---|---|---|---|---|
| `standing_s_total` | 69,083 | 69,291 | **−208** | **−0.14** | 9/15 |
| `regulated_s_total` | 11,355 | 2,003 | **+9,351** | **+5.96** | **15/15** |

Time at a stand is statistically identical between engines. The entire
measurable difference is regulated slow-running, 5.7× more of it, on every
seed. t = +5.96 is the largest effect anywhere in the day-13/14 data —
against throughput −2.84, fleet +2.01, premier +1.74.

This closes decision 4's open item, and closes it against the assumption in
which it was posed.

**Measurement limitation, stated because it matters below.**
`regulated_s` counts sim-seconds during which a binding regulation is in
force — `regulated_to_kmh < booked_limit`. It measures duration, not
severity, and does not distinguish a regulation that is the active
constraint from one masked by movement authority. A sharper decomposition
keys on which of `line_limit` and `stopping_limit_kmh` won. Not implemented.

### The mechanism: the emitter contradicts the model's own physics

`kinematics.absorbable_delay_s` defines the wait a train can shed without
stopping, given it will not be run below `MIN_REGULATION_FRACTION = 0.35`
of its speed. `optimizer_global` enforces exactly that:

    model.Add(slack[key] <= absorbable).OnlyEnforceIf(stopped[key].Not())
    model.Add(slack[key] >= absorbable + 1).OnlyEnforceIf(stopped[key])

So when slack exceeds what regulation can absorb, the model sets
`stopped = 1`. That is a decision to stand the train, taken because
regulation is physically insufficient.

`emit_directives` then reaches the stand-impossible branch — no station
between the train and the resource entry — and degrades the stand to a
`REGULATE`, passing the **full** slack to `regulated_speed_kmh`, which has
no floor and no reference to `MIN_REGULATION_FRACTION`:

    base   = distance_m / speed_ms
    target = distance_m / (base + wait_s)

For 12626 on seed 3: 15,000 m approach, 95 km/h, priced hold 7,959 s.
`tests/test_regulation_floor.py` reproduces it exactly on the unfloored
code: **6.333 km/h against a booked 95**, where the module's own floor for
that stock is 33.250. 6.7% of line speed. Two functions in the same module
disagree about whether a floor exists.

Two things make it permanent. `_drain_directives` attaches
`hold_expires_sim_s` to `HOLD_AT_LOOP` and `STAND_ON_MAIN` and **nothing** to
`REGULATE`; it clears only on `RELEASE` or on a later hold for the same
train. And global emits ~1 directive per approval, so nothing overwrites it.
Enumerate was accidentally protected by its own churn — 74.2 directives per
run across 35.3 approvals, versus global's 28.8 across 29.5.

Seed 3 logs **2,791** stand-impossible degradations in one run; 12626, 20172
and 12002 are 97% of them, all on the DOWN approach blocks BLK-111D–115D.
12626 finishes with `stand=0, stand_events=0, regulated=10,790 s` of a
10,800 s run — three hours of crawling, never stopped, 5.7 km covered.

A stand and a 6 km/h regulation are not equivalent actions. The stand puts
the train in a loop and frees the running line, which is what a loop is for.
The regulation leaves it on the main occupying every block it passes
through, in front of everything behind it.

### The six ablations

All at n=15, arm A, paired on seed. Arm B is engine-independent and never
approves, so it was not re-run.

| ablation | throughput Δ | t | +/−/= | fleet Δ | t |
|---|---|---|---|---|---|
| **A6 regulation floor 0.35 vs baseline** | **+0.178** | **+2.26** | **5/0/10** | −2,048 | −1.85 |
| A6 floor 0.35 vs enumerate | −0.156 | −1.97 | 2/7/6 | +1,150 | +1.32 |
| **A6b regulation floor 0.20 vs baseline** | **+0.000** | **+0.00** | **2/2/11** | +573 | +0.58 |
| A2 `APPROVAL_RULE=fingerprint`, global | −0.156 | −1.33 | 5/7/3 | +1,177 | +0.67 |
| A2 fingerprint, enumerate | −0.200 | −2.20 | 1/6/8 | +2,020 | +1.71 |
| A2 fp global vs fp enumerate | −0.289 | −1.99 | 3/9/3 | +2,355 | +0.98 |
| A3 `GLOBAL_HOLD_TIER=sum_hold` | +0.044 | +0.40 | 5/4/6 | −96 | −0.07 |
| A3 `GLOBAL_HOLD_TIER=off` | −0.089 | −0.60 | 3/7/5 | +1,696 | +0.86 |
| (day 13) baseline vs enumerate | −0.333 | −2.84 | 2/9/4 | +3,198 | +2.01 |

#### A6 — the regulation floor is the only intervention that worked

Floored at `MIN_REGULATION_FRACTION`, throughput improves on five seeds and
**worsens on none**. Seed 3 recovers from 0.000 to 1.000. It closes 53% of
the gap to enumerate (0.333 → 0.156), and the regression is no longer
significant:

    day 13   global vs enum   d −0.333  t −2.84  CI [−0.585, −0.082]  excludes zero
    day 14   floor  vs enum   d −0.156  t −1.97  CI [−0.325, +0.013]  includes zero

Stand-impossible degradations fall from 611 per run to 299, a 51% reduction.

**The floor does not change the solved plan.** `tests/test_descent.py`
reports identical holds, identical precedence, the same headline
`('12001','12002','SEC-PWL-KSV')` and `worst_hold = 14722 s` at
`REGULATION_FLOOR_FRACTION` 0 and 0.35. The change is entirely emitter-side:
it alters how a solved schedule is expressed as directives, not what is
solved. No banked ablation's plan is affected by it, which is the strongest
argument for applying it before the freeze.

The floor is not a tuned parameter. At
`wait_s == absorbable_delay_s(d, v, f)` the unfloored expression already
returns exactly `v·f`; the floor saturates the function at the precise point
`absorbable_delay_s` declares a stand is required, and nowhere earlier.

#### A6b — WITHDRAWN prediction: the floor is NOT robust to the fraction

Stated on day 14 before the run, as hard-fail row 1.2 of the draft gate:
`REGULATION_FLOOR_FRACTION=0.20` should also improve throughput, since it
still clamps the worst cases.

It does not. Mean throughput at 0.20 is **1.2222 — identical to the
unfloored baseline to four decimal places.** d = +0.000, t = 0.00, two seeds
better, two worse, eleven tied. Seed 3 stays at 0.000. Seed 15 goes
1.000 → 0.333 and seed 7 goes 1.000 → 0.667.

| arm | mean throughput | ≥ 1.333 | paired Δ vs arm B | 95% CI |
|---|---|---|---|---|
| enumerate | 1.5556 | 15/15 | +0.223 | [+0.109, +0.337] |
| baseline, floor 0 | 1.2222 | 8/15 | −0.111 | [−0.369, +0.147] |
| floor 0.20 | 1.2222 | 10/15 | −0.111 | [−0.412, +0.191] |
| floor 0.35 | 1.3999 | 12/15 | +0.067 | [−0.092, +0.226] |

This failed a hard-fail row in the day-15 draft gate. It is recorded as a
failure and the row was not amended after the fact. See `day15-gate.md`.

A physical reading exists — 0.20 × 95 = 19 km/h is still a crawl occupying
every block it passes, while 0.35 × 95 = 33 km/h clears the section; and
0.20 sits *below* the boundary the model enforces, so it permits a range
`absorbable_delay_s` has already ruled out and restores no consistency at
all. That reading was constructed after seeing the number and predicts
nothing that had not already happened. It is recorded as interpretation, not
evidence.

What the result does establish, negatively: the floor's effect is specific
to the value at which the two functions agree, not a general benefit of
clamping low regulations. Consistent with the consistency argument;
inconsistent with an "any floor helps" reading.

#### WITHDRAWN prediction: `regulated_s_total` under the floor

Stated on day 14: `regulated_s_total` would collapse toward enumerate's
~2,000 under the floor. It moved 11,355 → 10,402, t = −1.04, not
significant. The floor changes the regulation's *severity*, not its
*duration*, and the column measures duration. The column could never have
shown this fix. The mechanism is evidenced by the outcome metrics and the
halved degradation count, which is weaker support than the column would have
been.

#### A2 — the approval-rule hypothesis is falsified

Under `fingerprint`, global's application volume rises 11×:

    directives_submitted   28.8 -> 318.7   t +5.97   15/15 seeds
    approval_events        29.5 -> 335.5   t +6.27   15/15 seeds

Throughput does not recover. It moves −0.156, t = −1.33, not significant,
and in the wrong direction. Enumerate degrades under the same rule
(throughput t = −2.20, premier +2,507 t = +2.63), so it is not an
engine-specific artefact.

Global versus enumerate **under fingerprint** holds at −0.289, t = −1.99.
The `conflict_id`-only comparison was not biased against global. The day-13
comparison stands as run.

The hypothesis — a coordinated plan approved once and then diverged from,
versus an uncoordinated plan re-applied continuously — predicted recovery
under faithful application. Applying the coordinated plan continuously makes
both engines slightly worse.

#### A3 — the hold tier is not the mechanism, in either direction

Neither replacing min-max with min-sum nor removing the tier moves
throughput, fleet or premier. The one significant effect is that the worst
hold grows monotonically as the tier is weakened:

| tier | mean `worst_hold_s_max` | vs shipped | t |
|---|---|---|---|
| `worst_hold` (min-max, shipped) | 14,908 | — | — |
| `sum_hold` (min-sum) | 15,125 | +217 | +3.16 |
| `off` | 15,382 | +474 | +3.98 |

The tier does what a min-max objective is for, and doing it is not what
costs throughput. A4 predicted this: the tier governs how standing is
*distributed*, and there is no standing excess to redistribute (Δ = −208 s,
t = −0.14).

Under `sum_hold`, 40201 draws the largest priced hold in the plan
(15,178 s) and `emit_directives` gives it a `REGULATE` with 0 observed
standing across 0 episodes. Min-sum concentrates hold onto one train, and
concentration makes that train more likely to hit the stand-impossible path.
The proposed A3 fix feeds the A6 defect.

### WITHDRAWN: `_scenario_from` as the residual throughput mechanism

An earlier draft of this addendum argued that the surviving directive gap —
0.95 directives per approval against enumerate's 2.10, and
`uncovered_trains_total` 58.7 against 16.8 — pointed at the `_scenario_from`
two-predicate split as the remaining throughput mechanism. Withdrawn on all
three legs.

* **`uncovered_trains_total` is a per-card count.** `count_refusals.py:198`
  computes `uncovered = len(members - targeted - leads)` per conflict and
  sums across conflicts; `harness.py:373` then sums that across approvals. A
  globally-covered train reads uncovered on every card that does not target
  it. Under `fingerprint` the column reached 316.2 against 335.5 approvals —
  it tracks how often approve is pressed. It measures cards, not coverage.
* **The lower directive ratio is partly the design working.** Days 8–11
  deliberately changed emission to one stand per train plus one aggregated
  REGULATE reproducing the solved schedule. Enumerate's 2.10 includes its
  duplicates — 3.7 contradictions per run, trains receiving three directives
  from three independently-solved conflicts with disagreeing speed targets.
  At tick 1 `test_directive_replay` shows global emitting 9 directives across
  9 of 9 trains against enumerate's 13 with 5 uncovered: global covers more
  trains with fewer directives.
* **The harness approves every card**, so distributing directives across
  cards cannot withhold anything from the injector. `_scenario_from` is a
  redistribution mechanism and was being treated as a destruction mechanism.

The move — reaching for a plausible residual explanation after five
ablations, from a real correlation, without running the measurement that
would test it — is the same one `ab-enumerate.md` records for the
"~35 unreconciled local decisions" claim that day 13 falsified.

`_scenario_from` remains a real and demo-visible defect: roughly a third of
day-12 approvals produced a card naming a train, a loop and a duration with
an empty directive set while the engine reported IN_FORCE. It stays in WS-C,
days 15–16, where it fixes what it actually causes.

### Still unexplained after six ablations

47% of the throughput gap remains after the floor, and no tested hypothesis
accounts for it. The most likely residual named in this document is in
Limitations below: a stand-impossible situation should hold at the nearest
loop behind the train or accept the queue, and it currently does neither —
the floor makes the degraded regulation survivable without making it right.

Reporting instrumentation specified for the day-15 run, not an ablation:
`directives_per_evaluate` and `distinct_trains_covered_per_evaluate`, both
engines. If global emits six to eight directives across six to eight
distinct trains per evaluate while enumerate emits thirteen across eight, the
directives-per-approval gap collapses as an artefact of the counting unit.

### Limitations of this addendum

* The regulation floor is a diagnostic, not a fix. The correct repair is that
  a stand-impossible situation should hold at the nearest loop behind the
  train, or accept the queue — not emit a physically invalid regulation.
* The floor changes hold discharge in `test_directive_replay`: 12280 goes
  from cleared at 10,440 s to never, 40208 from 17,710 s to never, 40201
  from 12,270 s to 14,870 s. Not a gate — the hold-discharge post-condition
  is unasserted and `HARNESS-NOTES` records three predicates that misreport
  correctly-expiring holds. Carried to WS-E.
* "No longer significantly worse than enumerate" is not "as good as
  enumerate". At n=15 with these SDs the design is underpowered for
  equivalence claims, which is the same limitation the day-13 report was
  corrected for.
* Two predictions made inside this addendum failed and one claim was
  retracted in full. All three are marked WITHDRAWN above rather than
  removed.
* One scenario topology, three sim-hours, seed perturbation of initial
  conditions only. Unchanged from `ab-enumerate.md`.

### Instrumentation added on day 14

Six columns on `tests/harness.py` (`standing_s_total`, `regulated_s_total`,
`premier_standing_s`, `trains_held_gt0_mean`, `total_hold_var_mean`,
`worst_hold_s_max`), three counters on `TrainRuntime`, `GLOBAL_HOLD_TIER`
and `REGULATION_FLOOR_FRACTION` env knobs (both defaulting to shipped
behaviour), a CSV header guard on `append_row`, engine and knob provenance
in `run_ab.sh`, and `tests/test_regulation_floor.py` as the eleventh entry in
`run_all.sh` with four new fix-verification checks and two `dupe_check`s.

Proven measurement-only: re-running the committed `ab-global.csv` and
`ab-enumerate.csv` sweeps under the instrumented harness reproduced **0
mismatches** across all 20 pre-existing columns on both engines, and a
seed-7 re-run after an import cleanup matched all 26 non-timing columns
exactly.