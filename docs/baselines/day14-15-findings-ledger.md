# Day 14–15 — complete findings ledger

Every finding, its evidence, its location in the code, what was changed and
why, and every claim made during the work that turned out to be wrong.

Written at the end of day 15. Nothing here is inferred from reading code
alone unless the entry says so explicitly.

---

## PART 1 — CONFIRMED FINDINGS

### F1. The day-13 throughput conclusion was wrong

**Claim in the day-13 report:** "Throughput −0 — worse on 2/15."

**Measured**, paired on seed, arm A, `ab-global.csv` minus `ab-enumerate.csv`:

| metric | global | enumerate | arm B | Δ | t | b/w/t |
|---|---|---|---|---|---|---|
| throughput | 1.222 | 1.556 | 1.333 | −0.333 | −2.84 | 2/9/4 |
| fleet delay | 46,088 | 42,890 | 37,144 | +3,198 | +2.01 | 7/8/0 |
| premier delay | 11,741 | 9,437 | 12,352 | +2,304 | +1.74 | 4/11/0 |

95% CI on throughput [−0.585, −0.082], excludes zero. p ≈ 0.013.
corr(throughput Δ, fleet Δ) = −0.894. Reproduced twice by independent
scripts.

**Why the original was wrong:** it generalised the Premier SD of 5,120 s to
the whole apparatus. Throughput has SD 0.45 and is fully powered at n=15.
The design detected a difference; it was read as noise.

**Status:** corrected in writing below the original, per the amendment
convention in `ab-enumerate.md`.

---

### F2. The zero is trains held short of the section, not gridlock inside it

**Method:** instrumented every `SEC-PWL-KSV` entry and exit transition on
seed 3, both engines. The counter fires on transition *off* the resource, so
a zero was ambiguous between "entered and never left" and "never arrived".

**Measured**, global, seed 3, end of run. Section spans km 59–97 for DOWN,
from km 97 for UP:

| train | dir | final km | verdict |
|---|---|---|---|
| 12626 | DOWN | 49.73 | short of 59 |
| 12002 | DOWN | 39.52 | short of 59 |
| 12050 | DOWN | 46.85 | short of 59 |
| 20172 | DOWN | 43.18 | short of 59 |
| 12001 | UP | 82.55 | short of 97 |
| 12138 | UP | 80.85 | short of 97 |
| 12280 | UP | 76.85 | short of 97 |
| 54402 | UP | 72.85 | short of 97 |
| 40201 | DOWN | 70.60 | inside, never left |
| 40208 | UP | 109.08 | inside, never left |

Enumerate on the same seed: 12001, 12280, 54402 all finish at 96.95 and
12138 at 97.95 — queued at the entry — while 40201 reaches 148.4, 40208
reaches 138.0, and 12002 completes 195 km and recycles.

**Status:** answered. The counter is correct; the plan parks the fleet
upstream.

---

### F3. The excess is regulated slow-running, not standing

The decomposition open since day 5. Paired, n=15, global minus enumerate:

| column | global | enumerate | Δ | t | seeds worse |
|---|---|---|---|---|---|
| `standing_s_total` | 69,083 | 69,291 | **−208** | **−0.14** | 9/15 |
| `regulated_s_total` | 11,355 | 2,003 | **+9,351** | **+5.96** | **15/15** |

t = +5.96 is the largest effect in the entire day-13/14 dataset — against
throughput −2.84, fleet +2.01, premier +1.74.

**Instrumentation:** `simulator/injector.py`, `TrainRuntime.standing_s`,
`regulated_s`, `stand_events`, accumulated in `_advance`.

**Design decision — counted, not derived.** The obvious implementation
attributes the per-tick shortfall against `scheduled_distance_km`. That
quantity goes **negative** whenever a train recovers time, and a signed
number cannot answer "standing or slow-running". Time at a stand and time
under a binding regulation are both non-negative and directly countable.

**Design decision — `regulated_to_kmh < booked_limit`, not `is not None`.**
`booked_limit = min(scheduled_speed_kmh, _permitted_speed(train))` is in
scope. A REGULATE to 90 km/h on a 60 km/h link costs nothing; billing it
would make every REGULATE look expensive and would have inverted the answer.

**Design decision — the recycle branch is deliberately excluded.** The early
`return` in `_advance` for a train blocked at terminus also skips the
`scheduled_distance_km` advance, so such a train accrues **no delay**.
Counting it as standing would decouple `standing_s` from the delay it
exists to explain.

**Known limitation:** the column measures *duration* under a binding
regulation, not *severity*, and does not distinguish a regulation that is
the active constraint from one masked by movement authority. A sharper
decomposition keys on which of `line_limit` and `stopping_limit_kmh` won.
Not implemented.

---

### F4. Two functions in one physics module disagree about whether a regulation has a floor

**Location:** `shared/railsim/kinematics.py` and `ai-engine/optimizer_global.py`.

`absorbable_delay_s(d, v, f) = (d/v)(1/f − 1)` defines the wait a train can
shed without stopping, given it will not run below
`MIN_REGULATION_FRACTION = 0.35` of its speed.

`optimizer_global.py` enforces exactly that as a hard constraint:

    model.Add(slack[key] <= absorbable).OnlyEnforceIf(stopped[key].Not())
    model.Add(slack[key] >= absorbable + 1).OnlyEnforceIf(stopped[key])

So when slack exceeds what regulation can absorb, the model sets
`stopped = 1` — a decision to **stand** the train, taken because regulation
is physically insufficient.

`emit_directives` then reaches the stand-impossible branch (no station
between the train and the resource entry) and degrades the stand to a
`REGULATE`, passing the **full slack** to `regulated_speed_kmh`, which had
no floor and no reference to `MIN_REGULATION_FRACTION`:

    base   = distance_m / speed_ms
    target = distance_m / (base + wait_s)

**Measured**, `tests/test_regulation_floor.py`, seed-3 case
(15,000 m approach, 95 km/h, priced hold 7,959 s):

    saturation disabled -> 6.333 km/h    (the day-13 behaviour)
    saturated at 0.35   -> 33.250 km/h   (the module's own floor)

6.7% of line speed where the module's own floor is 35%.

**Frequency:** seed 3 logs **2,791** stand-impossible degradations in one
1080-tick run. 12626, 20172 and 12002 account for 97%, all on DOWN approach
blocks BLK-111D through BLK-115D. 12626 finishes with
`stand=0, stand_events=0, regulated=10,790 s` of a 10,800 s run — three
hours crawling, never stopped, 5.7 km covered.

**Why a stand and a 6 km/h regulation are not equivalent:** the stand puts
the train in a loop and frees the running line, which is what a loop is for.
The regulation leaves it on the main occupying every block it passes
through, in front of everything behind it.

**Status:** fixed by parameter collapse, F6 below.

---

### F5. REGULATE carries no expiry

**Location:** `simulator/injector.py`, `_drain_directives`.

`HOLD_AT_LOOP` and `STAND_ON_MAIN` both set `hold_expires_sim_s`.
`REGULATE` sets `regulated_to_kmh` and nothing else. It clears only on a
`RELEASE` directive, or on a later `HOLD_AT_LOOP`/`STAND_ON_MAIN` for the
same train.

**Consequence:** global emits ~1 directive per approval, so nothing
overwrites it — a regulation issued at tick N caps the train for the
remaining 1080 − N ticks. Enumerate was accidentally protected by its own
churn (74.2 directives per run across 35.3 approvals vs global's 28.8
across 29.5), which constantly clears regulations. This is why
`regulated_s_total` reads 370 s for enumerate and 24,170 s for global on
seed 3.

**Status: NOT FIXED.** Named, measured, left in place. Adding an expiry
changes production directive semantics and was out of scope for day 15.

---

### F6. The floor works, and it is the only intervention of six that did

`REGULATION_FLOOR_FRACTION=0.35` at n=15, arm A, paired:

    throughput  d +0.178  t +2.26  CI [+0.009, +0.347]  5 better / 0 worse / 10 tied
    fleet       d −2,048  t −1.85
    premier     d −1,218  t −1.31

**Better on five seeds, worse on none.** Seed 3 recovers 0.000 → 1.000.
Closes 53% of the gap to enumerate (0.333 → 0.156). The regression stops
being significant:

    day 13   global vs enum   d −0.333  t −2.84  CI [−0.585, −0.082]  excludes zero
    day 14   floor  vs enum   d −0.156  t −1.97  CI [−0.325, +0.013]  includes zero

Stand-impossible degradations fall from 611 per run to 299 — 51%.

**Gate row 1.1 goes from 8/15 to 12/15.**

**Status:** shipped. Implemented as the parameter collapse below.

---

### F7. The floor is NOT robust to the fraction — row 1.2 failed

**Pre-registered before the run:** `REGULATION_FLOOR_FRACTION=0.20` must
also improve throughput, sign only.

**Measured:** mean throughput 1.2222 — identical to the unfloored baseline
to four decimals. d = +0.000, t = 0.00, 2 better / 2 worse / 11 tied. Seed 3
stays at 0.000. Seed 15 goes 1.000 → 0.333, seed 7 goes 1.000 → 0.667.

| arm | mean | ≥1.333 | paired Δ vs arm B | 95% CI |
|---|---|---|---|---|
| enumerate | 1.5556 | 15/15 | +0.223 | [+0.109, +0.337] |
| baseline, floor 0 | 1.2222 | 8/15 | −0.111 | [−0.369, +0.147] |
| floor 0.20 | 1.2222 | 10/15 | −0.111 | [−0.412, +0.191] |
| floor 0.35 | 1.3999 | 12/15 | +0.067 | [−0.092, +0.226] |

**The row failed and was not rewritten.**

**Diagnostic error found later:** the 0.20 run floored the *emitter* at 0.20
while `absorbable_delay_s` and the model constraint stayed at 0.35. So it did
not test a floor at 0.20 — it tested a configuration where the two functions
still disagree, just by less. That is a post-hoc reading; it does not restore
the row.

**Post-hoc interpretation, recorded as interpretation not evidence:**
0.20 × 95 = 19 km/h is still a crawl occupying every block it passes;
0.35 × 95 = 33 km/h clears the section. And 0.20 sits *below* the boundary
the model enforces, permitting a range `absorbable_delay_s` has already
ruled out.

---

### F8. The approval-rule hypothesis is falsified

`APPROVAL_RULE=fingerprint`, n=15, both engines:

    directives_submitted   28.8 -> 318.7   t +5.97   15/15 seeds
    approval_events        29.5 -> 335.5   t +6.27   15/15 seeds

An 11× rise in application volume. Throughput moves **−0.156, t = −1.33**,
not significant and in the wrong direction. Enumerate degrades under the
same rule (throughput t = −2.20, premier +2,507 t = +2.63), so it is not
engine-specific.

Global vs enumerate **under fingerprint** holds at −0.289, t = −1.99. The
`conflict_id`-only comparison was not biased against global; the day-13
comparison stands as run.

---

### F9. The hold tier is not the mechanism, in either direction

| tier | mean `worst_hold_s_max` | vs shipped | t | throughput Δ | t |
|---|---|---|---|---|---|
| `worst_hold` (min-max, shipped) | 14,908 | — | — | — | — |
| `sum_hold` (min-sum) | 15,125 | +217 | +3.16 | +0.044 | +0.40 |
| `off` | 15,382 | +474 | +3.98 | −0.089 | −0.60 |

The tier does exactly what a min-max objective is for, and doing it is not
what costs throughput. F3 predicted this: the tier governs how standing is
*distributed*, and there is no standing excess to redistribute.

**Side observation:** under `sum_hold`, 40201 draws the largest priced hold
in the plan (15,178 s) and `emit_directives` gives it a `REGULATE` with 0
observed standing across 0 episodes. Min-sum concentrates hold onto one
train, and concentration makes that train more likely to hit the
stand-impossible path. The proposed A3 fix feeds the F4 defect.

---

### F10. `uncovered_trains_total` is a per-card count

**Location:** `tests/count_refusals.py:198`, `tests/harness.py:373`.

    uncovered = len(members - targeted - leads)   # per conflict
    uncovered_total += uncovered                  # summed across conflicts
                                                  # then across approvals

A globally-covered train reads uncovered on every card that does not target
it. Under `fingerprint` the column reached 316.2 against 335.5 approvals —
it tracks how often approve is pressed.

**Consequence:** any argument built on `uncovered_trains_total` comparing
engines is measuring cards, not coverage. See E3.

---

### F11. `total_hold_var_mean` is timing-sensitive

Seed 7, `day14-regfloor-global.csv` vs `day15-collapse-global.csv`:

| column | regfloor | collapse |
|---|---|---|
| every outcome column | identical | identical |
| `trains_held_gt0_mean` | 5.717 | 5.717 |
| `worst_hold_s_max` | 15,142 | 15,142 |
| **`max_solve_ms`** | **466.7** | **1382.1** |
| **`total_hold_var_mean`** | **8,456,753.2** | **8,460,745.1** |

Standalone re-run reproduced 8,456,753.2 exactly, nine times across three
invocations. `GLOBAL_TIER_BUDGET_S` is a per-tier **wall-clock** budget; a
loaded machine truncates a tier, which returns a different-but-equally-
feasible plan on one tick — same held count, same worst hold, same
directives, marginally different variance.

**Proven:** the column is timing-sensitive.
**Not proven:** that tier truncation is the mechanism. `counts["truncated"]`
exists in `optimizer_global` but is not a CSV column.

**Consequence:** `total_hold_var_mean` joins `max_solve_ms` on the excluded
list for every identity check. This is a defect in the day-14
instrumentation — a mean-over-solves column was added and then an identity
check written that assumed determinism.

---

### F12. Hold state is written at eight sites with four meanings

**Location:** `simulator/injector.py`.

| lines (pre-instrumentation) | meaning |
|---|---|
| 272–275 | RELEASE clears |
| 282–283 | REGULATE supersedes |
| 304–308 | STAND_ON_MAIN issues |
| 326–334 | HOLD_AT_LOOP issues |
| 451 | abandoned — station astern |
| 508–511 | berthed in loop |
| 535–539 | `_hold_discharged` fired, standing on main |
| 557–560 | `_hold_discharged` fired, in loop |

A predicate over `train.hold_station_id is None` sees one boolean across all
eight and cannot distinguish "never discharged" from "discharged and
re-issued". `HARNESS-NOTES` records three predicates that misreported on
correctly-expiring holds for exactly this reason.

**Fix:** `hold_seq` identity on `TrainRuntime`, a `_hold_event` log at every
write site, and a post-condition written against directive identity.

---

### F13. A berthed train with an occupied main correctly stays put

**Location:** `simulator/injector.py`, the in-loop release branch.

    if train.in_loop is not None and self._hold_discharged(train):
        main_clear = all(occ.get(r) in (None, train.train_id) for r in (head, tail))
        if main_clear and train.authority_km > train.distance_km + 0.5:
            # release

`_hold_discharged` returns True the moment expiry passes. The release is
then gated on the main being clear and half a kilometre of authority. **A
train in a loop cannot pull out onto an occupied running line.**

"Expiry passed and no terminal event" is therefore not a defect. The first
classifier written on day 15 flagged this correct behaviour as `HOLD-FAIL` —
the **fourth** misreporting predicate on this state machine, written by me
after the advisor had specifically warned that guessing a predicate is what
produced the first three.

**Fix:** `release_blocked` event logged on the refusal branch, edge-triggered
on `hold_block_logged_seq`. `held, exit blocked` is a pass.

---

### F14. The floor does not cause a discharge regression — Decision 1 resolved

Seed 3, corrected classifier:

| | issued | terminated | exit-blocked | **latched, no discharge path** |
|---|---|---|---|---|
| floor 0.35 | 19 | 13 | 8 | **0** |
| floor 0.001 | 17 | 11 | 1 | **1** (seq 17, 12050, t678) |

Across seeds 3, 7, 12 at the shipped fraction: **0 latched on every seed.**
The unfloored arm produces one latch. The tick-1 observation that started
this line of work — "12280 cleared at 10,440 s → never" — was a berthed
train waiting for a clear main, read through a flag predicate that cannot
see occupancy.

**Decision 1: the floor ships.**

**The genuine defect this exposed** (not caused by the floor): a
`HOLD_AT_LOOP` on a train that never reaches its loop has **no discharge
path at all**. `standing_on_main` is False so the stand branch never runs;
`in_loop` is None so the loop branch never runs. `discharged_stand` fired
zero times in the seed-3 run despite two `STAND_ON_MAIN` issues.

---

### F15. `GLOBAL_TIER_BUDGET_S` makes parallel harness runs nondeterministic

Seed 12, f=0.35, three invocations of the same test file: **25/19/7**,
**15/10/5**, **15/10/7** issued/terminated/blocked. Three concurrent
harnesses load the machine, the wall-clock per-tier budget truncates
unevenly, plans diverge.

**Fix:** the discharge gate lifts `GLOBAL_TIER_BUDGET_S` to 1000 so only
`GLOBAL_DET_BUDGET` binds. Deterministic time is reproducible across
machines and across load — the same argument `docker-compose.yml` already
makes for the production setting. Verified stable across three invocations.

---

### F16. Row 1.4 FAILS — drift grows, cause not established

In-container, 2,040 ticks, 4,093 s wall against 4,080 s expected at
`TICK_SECONDS=2.0`:

    evaluate  median 0.267s  p95 0.963s  max 5.005s
    over 2.0s: 19    over 4.0s: 1
    drift by quarter  +7.34  +8.53  +9.95  +12.03
    drift  first10 +0.057s   last10 +13.398s

Excluding the first 100 ticks, drift still runs 6.87 → 13.40 s.

Post-warmup by eighths:

| eighth | mean_eval | p95_eval | drift gain | gain/tick |
|---|---|---|---|---|
| 1 | 0.745s | 0.918s | +0.90s | 3.73 ms |
| 2 | 0.745s | 1.388s | +0.58s | 2.39 ms |
| 3 | 0.346s | 0.862s | +0.05s | 0.21 ms |
| 4 | 0.274s | 0.312s | +0.78s | 3.20 ms |
| 5 | 0.162s | 0.259s | +0.62s | 2.54 ms |
| 6 | 0.172s | 0.241s | +0.95s | 3.89 ms |
| 7 | 0.193s | 0.267s | +1.03s | 4.26 ms |
| 8 | 0.500s | 0.928s | +1.42s | 5.85 ms |

`mean_eval` falls 0.745 → 0.162 then rises to 0.500. `gain_per_tick` goes
3.73 → 0.21 → 5.85. **They do not track each other**, so the drift is not
driven by `evaluate` cost. Eighth 3 is essentially zero gain, so it is not
cleanly monotonic either.

**Established:** drift grows over the run; `evaluate` p95 post-warmup is
0.898 s, well inside one 2.0 s tick period; the growth does not correlate
with solver cost.

**Not established:** the mechanism. Candidates not tested — loop overhead
outside the timed region, detector per-train history growth,
`published`/`plan_in_force` accumulating keys, `xread` returning larger
batches as the engine falls behind (self-reinforcing), or host load on a
laptop running Docker plus the test suite for an hour.

**Row 1.4 fails as written.** The row says the backlog must be no larger at
the end than at the start. It is larger. Recorded, not amended.

**Note:** a single-call ceiling would have PASSED this — p95 0.963 s against
a 4.0 s bar. This is precisely why `DECISIONS.md` specifies lag stability
rather than a ceiling.

---

### F17. Row 1.5 FAILS — coverage breaks mid-run, on every seed

`tests/test_replay_seeded.py`, seeds 1–3, sampled at tick 1 and tick 540:

| seed | tick | directives | priced | **missing** | ordered | negative |
|---|---|---|---|---|---|---|
| 1 | 1 | 8 | 8 | 0 | 3 | 0 |
| 1 | 540 | 6 | 8 | **2** | 3 | 0 |
| 2 | 1 | 7 | 9 | **2** | 6 | 0 |
| 2 | 540 | 6 | 8 | **2** | 3 | 0 |
| 3 | 1 | 8 | 8 | 0 | 6 | 2 |
| 3 | 540 | 5 | 8 | **3** | 3 | 0 |

Missing trains: `12001` at t540 on **all three seeds**; `40208` at t540 on
**all three seeds**; `12280` on seed 3; `12050` and `12138` on seed 2 t1.

At tick 540 the plan prices 8 trains and emits 5–6 directives, consistently,
on every seed tested.

**This is the measurement the advisor asked for.** His falsification test
was: if global emits six to eight directives across six to eight distinct
trains per evaluate, the directives-per-approval gap collapses as an
artefact of the counting unit. It emits 5–6 across 5–6 while pricing 8. At
tick 1 coverage is mostly complete; at tick 540 it never is.

**Cause: not established.** Candidates, untested:
- the train's motivating-resource card is not among the raised conflicts, so
  no directive is emitted for it
- `emit_directives` reaches the stand-impossible branch and the regulation
  degradation also fails — e.g. the train is already at or past the
  resource entry, leaving `distance_m <= 0`
- `GLOBAL_MAX_STOPS=1` interacting with a train priced across two resources
- `cumulative_hold_seconds` sums slack across every resource while
  `emit_directives` issues at one motivating resource, so a train can carry
  priced standing that no single directive is responsible for

The last is documented behaviour in `test_directive_replay.py` and is the
most likely, but it does not obviously explain why tick 1 covers and tick
540 does not.

**Why the old gate never saw it:** `test_directive_replay.py` asserts
coverage at tick 1 on the unperturbed scenario only. A run is 1080 ticks and
the stand-impossible degradation fires throughout — 2,791 times on seed 3
alone. Tick 1 is a boot-state gate.

---

### F18. Row 1.6 FAILS — two negatives survive an adequate watch

After correcting the watch window (E12), 4 of 6 negatives cleared. Two
survive:

    seed 3 t1: 40201 ordered to stand, observed minus priced = −5,767
    seed 3 t1: 40208 ordered to stand, observed minus priced = −841
    watch 18,301s against a max priced hold of ~14,700s

Both are freights, both on the perturbed seed 3, both with adequate watch.
`test_directive_replay.py` shows all `<- ordered` trains positive on seed 0
(unperturbed), so this is a perturbation-visible failure the tick-1
unperturbed gate cannot see.

**Cause: not established.** The `cumulative_hold_seconds`-vs-single-
directive mismatch documented in `test_directive_replay.py` is the leading
candidate, but that mismatch is supposed to be excluded by asserting only on
non-REGULATE (`<- ordered`) trains, which these are.

---

## PART 2 — CHANGES MADE, AND WHY

### C1. `simulator/injector.py` — A4 decomposition counters

Three fields on `TrainRuntime` (`standing_s`, `regulated_s`,
`stand_events`), accumulated in `_advance`.

Reasoning: see F3. Counted not derived; binding-regulation test not
`is not None`; recycle branch deliberately excluded.

**Proven measurement-only:** re-running the committed `ab-global.csv` and
`ab-enumerate.csv` sweeps under the instrumented harness reproduced **0
mismatches** across all 20 pre-existing columns on **both** engines.

---

### C2. `ai-engine/optimizer_global.py` — `GLOBAL_HOLD_TIER` ablation

`worst_hold` (shipped default) | `sum_hold` | `off`.

- `expected_solves` moved with the ladder, or `counts["tiers_total"]` lies
  and `test_descent.py` reports spurious truncation on a completed run.
- `sum_hold` gets its own `IntVar` bounded at `horizon × n_trains` so the
  `model.Add(expr == solver.Value(expr))` freeze between tiers has something
  to reference.
- `worst_hold` remains the untouched default. This is an ablation, not a
  change of default.

---

### C3. `ai-engine/optimizer_global.py` — `LAST_PLAN_STATS`

Module-level stash read by the harness, populated **before** the feasibility
return so an infeasible tick reads `feasible: 0.0` rather than leaving a
stale row from the previous tick.

Reasoning for a stash rather than a return-value change: `optimize_global`
returns `conflict_id -> [scenario]`, which is the contract `evaluate()`
publishes. A measurement column must not widen it.

---

### C4. `tests/harness.py` — six columns, trace, header guard

New columns appended **after** `seed_attempts` so every existing column
keeps its index and prior analysis scripts work by name and by position.

`HARNESS_TRACE_THROUGHPUT` records entries as well as exits, because the
counter fires only on transition off the resource and a zero is otherwise
ambiguous (F2).

`getattr(train, "standing_s", 0.0)` rather than attribute access, so the
harness still runs against an older injector while bisecting.

A3 discriminators are means across feasible ticks, not last-tick values — a
single tick's plan is not the run.

**Header guard on `append_row`:** `csv.DictWriter` will append 27-column
rows under a 20-column header without complaint, producing a file where
column 21 means two different things above and below one line. Refusing is
cheaper than discovering it in analysis. This was not requested and is the
highest-value line in the day-14 patch.

---

### C5. `tests/run_ab.sh` — engine and knob provenance

The env file recorded `cap/workers/budget/limit/rule/jitter` and **not which
engine ran**. Six CSVs were about to be written differing only by engine and
hold tier.

---

### C6. `shared/railsim/kinematics.py` — parameter collapse

Two constants that had to agree by convention became one:

    MIN_REGULATION_FRACTION = float(os.getenv("MIN_REGULATION_FRACTION", "0.35"))

    def regulated_speed_kmh(distance_m, speed_ms, wait_s,
                            min_fraction=MIN_REGULATION_FRACTION):
        ...
        return max(ms_to_kmh(distance_m / (base + wait_s)),
                   ms_to_kmh(speed_ms) * min_fraction)

- `min_fraction` is a parameter defaulted from the same constant
  `absorbable_delay_s` uses. **There is no second constant to tune**, so
  "is 0.35 tuned?" becomes a question about the physics module, where it
  predates day 14.
- `max()` rather than a branch: at the boundary both return the identical
  value (verified to 1e-9), so there is no discontinuity to reason about.
- `REGULATION_FLOOR_FRACTION` deleted from production. The unfloored
  ablation is `min_fraction=0.0` as an argument in the test, so the
  discrimination evidence lives in the test file rather than in an env var
  someone can flip.
- The env override moved onto `MIN_REGULATION_FRACTION` itself, so any
  future sensitivity run changes both functions together and **cannot
  reproduce the 0.20 mistake** (F7).
- `0 < f <= 1` guard: `absorbable_delay_s` computes `(1/f − 1)`, so an env
  override of 0 — the natural thing to try for the unfloored arm — divides
  by zero at import. Failing loudly beats a `ZeroDivisionError` three
  frames down.

**Proven behaviour-preserving:** re-ran the floored sweep at n=15 against
the banked one. **0 mismatches on 25 of 27 columns**; the two exceptions are
`max_solve_ms` and `total_hold_var_mean`, both timing-sensitive (F11).

**Cost, recorded:** the unfloored configuration is now unreachable in
production. `day14-baseline-global.csv`, `notier`, `sumhold` and both
`fingerprint` arms can never be re-run as written. They stay banked as CSVs.

---

### C7. `tests/test_regulation_floor.py` — the floor gate

Four assertions:
1. boundary identity — algebraic, passes unfloored, a **canary not a gate**
2. **saturation past the boundary** — the gate; red on unfloored code
3. no early bind below the boundary — stops a future "fix" raising the floor
   into the regulating range, which would make it a tuned parameter after all
4. discrimination — same inputs with `min_fraction=0.0` must return **below**
   the floor, so the gate fails if it ever stops discriminating

Plus the observed seed-3 case as data, not a synthetic.

**Verified:** exit 1 with 13 saturation failures unfloored; exit 0 at 0.35.

---

### C8. `simulator/injector.py` — hold-event trace and `hold_seq`

`SIM_TRACE_HOLDS=<path>` logs every hold-state mutation with the hold's
identity. Eight call sites, four meanings (F12).

- **Log AFTER the write for an issue, BEFORE the write for a clear**, so the
  record always shows the hold that was in force.
- `superseded_by_hold` on both issue paths, logged before the new `hold_seq`
  is assigned, so the outgoing hold's identity survives. Without this a
  second `HOLD_AT_LOOP` silently orphans the first.
- `berthed` is **edge-triggered** on `was_in_loop is None`. Level-triggered
  it fired every tick the train sat at the berth: **5,040 of 5,068 events**
  in the first trace.
- `release_blocked` edge-triggered on `hold_block_logged_seq`, same reason.
- `_discharge_reason` distinguishes `timeout` from `leader_passed`. A hold
  that times out at the 1800 s default is not the same event as one that
  discharges because the leader cleared, and the code could not tell you
  which.
- Written to a JSONL path via `atexit`, so it works from any caller with no
  harness change.

---

### C9. `tests/test_hold_discharge.py` — row 1.7 in the loop

- **Parallel `Popen`.** Six sequential 1080-tick runs hit `run_all.sh`'s
  300 s ceiling (`exit=124`). Three parallel finish in ~121 s.
- **`ASSERTED` vs `REPORTED` fractions.** The 0.001 arm is discrimination
  evidence, exactly as assertion 4 in the floor gate is. Asserting on it
  makes the suite permanently red against a configuration deliberately
  replaced.
- **`RUN_END_S` derived from `TICKS`**, not hardcoded — a hardcoded 10800
  silently misclassifies every hold if the tick count changes.
- **Trace deleted before launch** — a crashed run otherwise leaves the
  previous trace on disk and the classifier reads stale data.
- **`GLOBAL_TIER_BUDGET_S` lifted** so only deterministic time binds (F15).
  Verified stable across three invocations.

---

### C10. `ai-engine/main.py` — lag instrumentation

`AI_TRACE_LAG=<path>` records per-tick `wall_s`, `evaluate_s`, and
`drift_s = wall − ticks × TICK_SECONDS`.

- Drift, not a per-call ceiling: `DECISIONS.md` is explicit that a p95 under
  one tick period says nothing about whether the engine is falling behind.
  F16 proves the point — p95 0.963 s would have passed a ceiling gate.
- `_record_lag` sits **outside** the `try`. A failed evaluate still consumed
  wall time and still counts toward drift; excluding it would report the
  engine as keeping up precisely when it isn't.
- `TICK_SECONDS` added to the ai-engine service in compose — it was on the
  simulator only, so drift would have been computed against a default.

---

### C11. `tests/run_all.sh` — twelve tests, eight new checks

New loop entries: `test_regulation_floor.py`, `test_hold_discharge.py`.

New checks:
- `D14 floor gate exists`, `D15 discharge gate exists` — mirror
  `D11 replay gate exists`; catch a gate file being deleted or renamed,
  which `run_test` would otherwise report as a generic failure.
- `D14 gate discriminates` (`min_fraction=0.0`) — stops the discrimination
  evidence being deleted from the test.
- `D14 emitter saturates` (`min_fraction: float = MIN_REGULATION_FRACTION`)
  — stops the parameter being dropped back to a hardcoded constant, which
  would silently recreate the two-constant defect the collapse prevents.
- `D15 no separate floor constant` (absent `REGULATION_FLOOR_FRACTION`) —
  the `present`/`absent` pair is how the rest of the block already works.
- `D15 hold identity present`, `D15 blocked release logged`.
- `dupe_check MIN_REGULATION_FRACTION` — a duplicated constant raises no
  error and the later definition silently wins.

---

### C12. `tests/test_replay_seeded.py` — rows 1.5 and 1.6

- Imports `build_injector` from `tests/harness.py` rather than
  reimplementing `perturb`. Two day-2 defects came from measurement tooling
  re-deriving a production rule slightly differently and being masked by a
  tick where the rules agreed; `solvable_conflicts` was extracted from
  `evaluate()` for the same reason.
- Samples at tick 1 **and** tick 540. Tick-1-only is a boot-state gate (F17).
- Watch window derived from the plan (`max(priced) + 3600`, floor 9,000),
  the same rule `test_directive_replay.py` uses. A fixed window measures the
  watch rather than the plan (E12).
- 1.6 asserts only on non-REGULATE trains. A train carrying priced standing
  that receives a REGULATE is compared against a quantity it was never
  given, which is how 12138 read −5,119 on day 13.
- 23 s for six samples → ~115 s for the full 15×2, inside the 300 s timeout.

---

## PART 3 — CLAIMS MADE DURING THE WORK THAT WERE WRONG

Recorded because the pattern matters more than any individual error: every
one came from reasoning about code rather than measuring it, and every one
was caught by a check that ran in seconds.

**E1. "87% of the intervention time is standing."** Read a *level* as a
*difference*. Standing is ~69,000 s under **both** engines; the difference is
−208 s. Caught by running the paired analysis.

**E2. "`regulated_s_total` will collapse under the floor."** It moved
11,355 → 10,402, t = −1.04, not significant. The column measures duration,
the floor changes severity. Caught by the measurement.

**E3. "`_scenario_from` is the residual throughput mechanism."** Retracted on
all three legs — `uncovered_trains_total` is per-card (F10), the lower
directive ratio is partly the design working, and the harness approves every
card so distribution cannot withhold anything. Caught by the advisor, then
confirmed against `count_refusals.py:198`.

**E4. "Row 1.3 is the gate on the day-14 change."** The boundary identity is
algebraic and passes unfloored — the addendum said so two paragraphs
earlier. A section-1 label on a section-2 fact. Caught by the advisor.

**E5. "The floor changes emission only; it cannot alter any banked
ablation."** False, and A6 is the disproof: throughput moved +0.178. The
emitted directive changes the railway, which changes the next tick's input,
which changes every subsequent plan. By `DECISIONS.md`'s own rule the floor
is tier 0. Caught by the advisor.

**E6. `berthed` logged level-triggered.** 5,040 of 5,068 events. Caught by
reading the first trace.

**E7. "The latched holds will read never-berthed."** All four read
**berthed**. The prediction inverted the mechanism. Caught by the data.

**E8. The first discharge classifier flagged correct behaviour as
`HOLD-FAIL`** — the fourth misreporting predicate on this state machine,
after the advisor had warned that guessing a predicate is what produced the
first three. Caught by reading the release path properly (F13).

**E9. "The logging-only patch broke the release path."** Wrong diagnosis;
lines 639–652 were correct. Caught by asking for the grep instead of
asserting.

**E10. Appending the lag block after `if __name__ == "__main__"`.** The
container executes `main.py`, `main()` blocks forever, and everything below
never runs — `NameError: name '_record_lag' is not defined`. The
verification **imported** the module, where `__name__ != "__main__"` and the
file reads to the end. **Verified the wrong code path.** Caught by the
container.

**E11. `total_hold_var_mean` included in the identity check.** A
mean-over-solves column added on day 14, then an identity check written that
assumed determinism. Caught by the collapse re-sweep (F11).

**E12. Fixed 9,000 s watch in the replay gate.** Four of six 1.6 negatives
were the window truncating holds priced at 14,000 s+. Caught by comparing
against `test_directive_replay.py`'s derived window.

**E13. Row 1.4 read as "startup transient plus flat tax", then as
"monotonic and accelerating".** Neither survived the eighths table.
Caught by finer bucketing.

---

## PART 4 — GATE STATUS

| row | status |
|---|---|
| 1.1 throughput, 12/15 **and** CI lower bound > −0.333 | **PASS** (12/15; CI [−0.092, +0.226]) |
| 1.2 floor sensitivity at 0.20 | **FAILED**, recorded, not rewritten |
| 1.3 emitter/physics saturation | moved to section 2, verified to discriminate |
| 1.4 lag stability in-container | **FAILED**, cause not established |
| 1.5 replay coverage across seeds | **FAILED**, 2–3 missing at t540 on every seed |
| 1.6 ordered-train positivity across seeds | **FAILED**, 2 survive an adequate watch |
| 1.7 hold discharge | **PASS**, in the loop, deterministic |
| card/plan agreement | moved to day 16 |

Suite: 12 tests, 0 fix checks broken, 0 failures at the shipped
configuration.

Decisions resolved: the regulation floor ships (F6, F14).

**Three hard-fail rows are failing.** The decision rule as written covers
only 1.1 and 1.2. It does not say what happens when 1.4, 1.5 and 1.6 fail,
and that is the question for the advisor before anything is tagged.

new findings for: the single silent drop path with its 42/246 population; the re-target concentration at MTJ; the REGULATE partial state clear (F12's ninth site); _next_loop_station → None as a second silent path; the gate's REPLAY-FAIL summary truncating its own failure list; optimizer_global.py absent from run_ab.sh's preflight. New changes for the reason codes and the two additive directive fields, both proven inert on 25 and 26 columns. Part 3 gets E14: my train vs train_id query error, caught by a positive control, conclusion retracted.