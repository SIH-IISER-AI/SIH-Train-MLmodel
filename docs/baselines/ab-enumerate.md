# Enumerate baseline — A/B against no intervention

Tag: `day5-parity` · Data: `ab-enumerate.csv` (32 rows) · Config:
`ab-enumerate.env.txt` · Harness: `tests/harness.py`, `tests/run_ab.sh`

This is the arm the global model must beat on day 14. It is committed with its
own failure modes documented, because a baseline that hides them is not a
baseline.

## Method

Two arms over the same simulated 3 hours (1080 ticks × 10 sim-seconds),
headless, no Redis, no wall-clock pacing:

* **Arm A — enumerate + approve.** `optimize_precedence()` runs each tick;
  OPT-1's directives are submitted to the simulator.
* **Arm B — enumerate + no approve.** The solver runs and is scored
  identically; nothing is submitted. The fleet is left to the simulator's
  greedy priority rule.

Arm B is *not* "engine off". It is the control: same code path, same audits,
same CSV columns. The only difference is whether directives reach the injector.

Sixteen scenarios: seed 0 is `data/scenario10.json` unperturbed; seeds 1–15
shift each train's `start_offset_km` by ±2 km and `initial_delay_seconds` by
±300 s from a seeded RNG, redrawing on an illegal start. Statistics below use
seeds 1–15 (n=15) and are paired by seed.

Approval fires at most once per `conflict_id`, in both arms — in arm A it also
submits. `conflict_id_for()` hashes the sorted train set, so a train joining or
leaving the contention mints a new id: 6 distinct conflicts at tick 0 become
29–41 approval events over a full run.

Two production wall-clock deadlines are lifted in the harness
(`ENUMERATION_BUDGET_S`, `SOLVER_TIME_LIMIT_S`) and replaced by CP-SAT's
`max_deterministic_time`. A wall-clock deadline makes the *result* depend on
machine load, not just the timing column. Recorded in `.env.txt`.

## Result

Paired differences, arm A minus arm B, seeds 1–15:

| metric | arm A | arm B | Δ | 95% CI | p |
|---|---:|---:|---:|---|---:|
| Premier delay (s) | 9,437 | 12,352 | **−2,916 (−23.6%)** | [−4,763, −1,069] | 0.004 |
| Fleet delay (s) | 42,890 | 37,144 | **+5,746 (+15.5%)** | [+4,108, +7,384] | 3×10⁻⁶ |
| Non-Premier delay (s) | 33,453 | 24,792 | +8,662 (+34.9%) | — | — |

Premier: 13 of 15 seeds favour arm A (sign test p ≈ 0.007). Failures are
seeds 4 (+670 s) and 14 (+1,735 s).

Fleet: arm A is worse on **all 15** seeds, without exception.

Throughput through `SEC-PWL-KSV`: arm B is 1.333/sim-hour on every seed.
Arm A is better on 9, equal on 6, worse on 0 (sign test p ≈ 0.004), peaking at
2.0 on seed 12. In absolute terms this is 4 clearances versus 5 or 6 over three
sim-hours — a real effect, but too few events to quote as a percentage.

## The fleet cost is bad plans, not the priority rule

Premier delta and fleet delta correlate at **r = 0.71 (p ≈ 0.003)**.

| seed | Premier Δ | fleet Δ |
|---|---:|---:|
| 12 | −8,069 | +2,357 |
| 6 | −7,945 | +2,524 |
| 9 | −7,819 | +2,969 |
| … | | |
| 14 | +1,735 | +4,886 |
| 4 | +670 | +11,804 |

The seeds where the engine protects Premier best are also the seeds where it
costs the fleet least. A genuine priority trade-off would correlate
*negatively*: more Premier protection bought with more fleet delay. The
observed sign is the opposite.

So the +5,746 s is not the price of the IR precedence rule. It is the cost of
plans assembled from ~35 independent, locally-optimal decisions with nothing
reconciling them. This is the falsifiable claim the global model is built
against: if a single-model solver improves Premier delay *without* the fleet
regression, the diagnosis was right.

## Structural defect: the anti-starvation cap cannot see starvation

`DEFAULT_MAX_HOLD_SECONDS = 900`, applied per solve as
`cap_s[i] = forced_s[i] + max_hold_s` (`optimizer.py`). It bounds discretionary
delay *within one conflict*.

Seed 1, arm A, per train:

| train | arm A | arm B | Δ |
|---|---:|---:|---:|
| 40208 FREIGHT | 9,572 | 5,484 | +4,088 |
| 12626 EXPRESS | 5,633 | 793 | +4,840 |
| 40201 FREIGHT | 4,986 | 1,771 | +3,215 |

40208 exceeds the 900 s ceiling by 4.5×. `policy_exceeded` reads **0 on all 32
rows**. No individual solve breached the cap; ~35 compliant solves composed
into an unbounded total.

A zero in that column is therefore not evidence the policy works. It is
evidence the policy is scoped to the wrong quantity. No per-conflict solver can
express a cumulative constraint, which is why `total_hold[t]` over the whole
window is a new decision in `GLOBAL_MODEL_SPEC.md` and a day-14 gate row.

**Open:** the per-train figures above are *total* delay. Whether 40208's excess
is stands or regulated slow-running is not yet decomposed, and it determines
whether the global constraint should sum hold time or total discretionary
delay.

## Other measurements

* **Cap 5 discards half the problem.** `largest_group` reaches 9 on most seeds
  and 10 on seeds 12–14 — the entire fleet contending one resource.
  `MAX_TRAINS_ENUMERATED = 5` drops the rest, and the dropped trains are the
  freights and expresses that would be given way. 10! is 3.6 million
  permutations; the global model carries 10 trains in 90 booleans.
* **`policy_exceeded = 0` everywhere**, including on 10-train contention with
  hours of queueing. See above for why this is not reassuring.
* **`refused_directives` and `uncovered_trains` are confounded by arm.** Arm B
  shows ~3× arm A's refusals (≈45 vs ≈15 per run) because uncontrolled trains
  sail past their hold stations. These are not engine-quality metrics; read the
  `_t0` columns instead.
* **`contradictory_instructions_total` understates.** The per-approval audit
  only sees conflicts firing in the same tick, and after tick 1 approvals
  mostly arrive alone. `_t0` (2–4 trains) is the real figure.
* **Determinism.** Every non-timing column reproduces byte-for-byte across
  processes, hours apart, and across `PYTHONHASHSEED`. Verified by the gate in
  `run_ab.sh` and by independent re-runs of seeds 1 and 6 in both arms.
* **`max_solve_ms` (223–1,646 ms) is not comparable to the day-2 bench.**
  Day 2 reported a median of 5 repeats on one conflict; this is a max over
  thousands of calls and catches every GC pause. Use a p95 if this ever becomes
  a gate.

## How these numbers were arrived at, including the wrong turns

Recorded because the corrections are part of the evidence.

1. **A pre-parity run at n=5 showed Premier −24.8% on 5 of 5 seeds.** It was
   underpowered against an SD of 2,344 s and should not have been treated as a
   result.
2. **A physics defect was then found.** `_prepare()` computed
   `earliest_arrival_s` as one accelerating integral over the whole approach at
   the *destination* block's line speed, ignoring every intermediate speed
   restriction (the line runs 60/110/100/130/100/130/75/130/60 km/h). Measured
   drift against `detector.project()` was up to 207 s, and it inverted the
   12050/12138 arrival order — an input to `forced_s` and hence to the
   anti-starvation baseline. Fixed by passing `window.t_in` through;
   `tests/count_intervals.py` asserts parity.
3. **Post-fix at n=5 the Premier effect vanished** (p ≈ 0.27, arm A losing on
   2 of 6 rows). Arm B's outcome columns were byte-identical pre- and
   post-fix — confirming the control is engine-independent and the apparatus
   was sound.
4. **At n=15 the effect returned and is significant** (p ≈ 0.004). The n=5
   verdicts in both directions were noise.
5. **A withdrawn finding.** Pre-parity, seed 0 was the only favourable trade
   and was described as a scenario that flattered the engine. Post-parity seed
   0 is one of three losing rows. That characterisation was an artefact of the
   physics defect and does not hold.

## Limitations

* One scenario topology; perturbation varies initial conditions only.
* Three sim-hours is too short to resolve throughput beyond counts.
* Arm A's outcome spans 4,113–14,296 s on Premier (a 3.5× range) against arm
  B's 11,873–12,695 s (7%). The engine is far more sensitive to initial
  conditions than the greedy baseline; the mean is not the whole story.
* Arm B's *outcome* columns are engine-independent — proven across the parity
  fix — so future engine comparisons need arm A runs only.