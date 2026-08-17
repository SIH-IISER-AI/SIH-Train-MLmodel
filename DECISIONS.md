# DECISIONS.md

Non-obvious design choices in this codebase, and why the obvious alternative is
wrong. Written so a reader does not "fix" something that only looks broken.

Phases 1–2 predate this record. Phase 3 established Redis as the state
substrate; Phase 4 closed the epoch loop and rebuilt the recommendation; Phase 5
fixed twelve defects found by running the system rather than reading it.

---

## State and lifecycle

**Static data lives in Redis keys, not streams.** A stream broadcast is consumed
once. An engine booting a moment late finds nothing and deadlocks silently. A key
is readable at any time by any consumer in any order — the startup race is
structurally absent, not merely mitigated.

**Every commit carries an epoch** (`<boot seconds>-<sha1(network||scenario)[:10]>`),
stamped on the topology envelope and on every tick. Key-existence is not proof of
freshness: with AOF persistence an engine could hydrate a stale topology from a
previous run and project new trains onto an old track graph — no crash, just
confidently wrong conflicts. The timestamp component rotates on every restart
even with identical data, deliberately: a restart resets train positions, so
tracked state must be discarded regardless.

**The whole fleet is one Redis hash**, field per train. Per-train keys need
SCAN + N×HGETALL non-atomically at boot and force `route` into a delimited string.
Strictly dominated.

**Static state commits in one transaction, and reads detect tearing.** An engine
polling between a topology write and a fleet write hydrates zero trains and never
recovers — it has no reason to poll again. `read_static_state` re-checks the epoch
after reading both keys.

**Boot order: validate → commit → signal.** `_assert_legal_start` raises when two
trains seed inside one interlocking resource. Committing first would leave a
topology key with no simulator behind it. `SYSTEM_READY` publishes last so it is a
barrier, not a claim.

**The engine gates on the topology key, not on `SYSTEM_READY`.** Gating on the
control stream reintroduces the miss window that keys exist to remove. The key is
the barrier; the event is a notification.

**Epoch mismatch triggers a full rebuild, not reconciliation** — including
resetting the stream cursor to `$`, since leftover telemetry is pre-restart data.

**Redis persistence is off.** The simulator rebuilds everything on boot and
epoch-fencing makes anything older than `SYSTEM_READY` unreadable by design.
Nothing on that disk is ever read back. `docker compose down -v` is the standard
teardown.

**Errors degrade a tick, never the process.** `evaluate()` and `ingest()` swallow
and log. A long-running consumer that dies on one malformed packet dies mid-demo.

---

## Contracts and the write path

**The write path rejects directives from a dead epoch.** The subtle part:
`conflict_id_for()` hashes `resource_id | sorted(train_ids)` with nothing
epoch-derived, so the same `CONF-XXXXXXXX` regenerates across runs from identical
seed data. Filtering the *lookup* by epoch therefore silently re-targets a stale
click to the current recommendation for the same id — the controller approves a
plan they never read. Hence the epoch on `ControllerAction` itself, carrying **the
card's epoch, not the current clock**. Stamping the live epoch would compare a
value against itself.

A *missing* epoch is allowed, so the simulator can ship ahead of the frontend.

**A scenario with no directives does not claim the conflict resolves itself.**
The `no_op` reason read "natural precedence already resolves this conflict",
which is true for the natural-precedence case and a false safety claim for a plan
the simulator could not execute.

**Backfill is a state snapshot floored at `SYSTEM_READY`, not a log replay.** The
client keys conflicts by `conflict_id` and trains by `train_id`, so only the last
entry per key survives — the backfill's job is to reconstruct current state.
Walking backwards with dedupe spends a bounded budget on distinct keys instead of
replaying thousands of superseded fixes. `control_stream` is also in the live
cursor set, so a mid-session simulator restart pushes `SYSTEM_READY` down an
already-open socket.

**An empty directive list is a plan, not a lookup failure.** `_apply_action` tests
cache *membership*, distinguishing `None` (not found → reject) from `[]` (natural
precedence already works → acknowledge). Under a truthiness test the top-ranked
recommendation was un-approvable.

**The simulator acknowledges every controller action** via
`CONTROLLER_ACTION_RESULT` on `control_stream`. The client marks a conflict
pending on dispatch and retires it only on the verdict, so a rejection no longer
looks identical to an application, and the retirement survives a refresh.
A verdict that never arrives is reaped after 10s.

**Socket ownership is checked by identity.** `if (socketRef.current === ws)`.
React StrictMode double-mounts in dev; close events are async, so socket #1's
handler landed after mount #2 claimed the ref and nulled it. The dashboard
rendered live data while the write path was dead. This never worked in dev and
would have worked in `next build`, which is why it survived.

---

## Physics

**One kinematic model serves detection and optimization.**
`traverse_seconds_accelerating` is the single implementation. Previously the
detector accelerated and the optimizer held speed flat, so the same telemetry
produced 2446s and 3256s of occupancy for the same train — the detector's windows
overlapped, the optimizer's didn't, and the engine published a CRITICAL conflict
whose optimal resolution was zero delay for everyone.

**Trains run at booked speed, not the rake's design limit.** `max_speed_kmh` is
what the rake can do; `scheduled_speed_kmh` is what it is timetabled for. Running
at max made regulate advice nonsensical (telling a driver to speed up), let
trains permanently bank slack against the timetable, and shortened every
projected occupancy. Both values cross the wire under honest names; only the
booked one is used.

**The schedule reference is line-limited at its own coordinate.** The ghost train
and the physical train diverge by tens of kilometres once delay accrues, so
evaluating permitted speed at the physical position to advance a theoretical one
is a coordinate error. `_permitted_speed_at(train, at_km, reference_kmh)` takes
both. The reference is slowed only by permanent restrictions — never by a hold, a
red, or a regulation order — so `delay_seconds` measures what dispatch cost and
nothing else.

**Recovery running is deliberately absent.** A late train does not target its
maximum to make up time. Adding it to the solver without adding it to the
simulator creates model-plant mismatch: occupancies come out short, holds are
undersized, and the conflict re-forms after approval. If it is ever added it goes
in the injector first, then the detector, then the optimizer — all three or none.

---

## Detection

**The projection is not a pure function of physical state.** It reads committed
dispatch intent. Three regimes: standing in a loop (bounded loop occupancy,
running line released, resume from a stand); hold accepted but still running (walk
to the station, loop occupancy, resume); standing on the running line (current
window's `t_out` extends to release, resume).

**Intent and realisation are separate fields.** `hold_station_id` is set when a
directive lands; `in_loop_id` when the rake berths. Neither is derivable from the
other or from speed — a train stopped at a red en route to its holding station
looks identical to one standing in the loop if you only have speed and
`schedule_status`. This is why `blocked` no longer triggers on `HELD`.

**Loop occupancy is bounded, never pinned to the horizon.** Pinning it resolves
the conflict by blinding the projection to the pull-out, which on a single line is
the most dangerous movement in the scenario.

**Release times are read off the other train's own projection, not re-derived.**
Two kinematic estimates of the same clearance disagree wherever line speed changes
at a resource boundary — a 28-second disagreement was enough to leave a conflict
standing after the hold had resolved it. One number, one derivation. This makes
per-train projections mutually dependent: there is a memo cache invalidated on
ingest and a recursion guard for hold cycles.

**A blocked train's projection is bounded, not truncated.** Returning a single
window to the horizon and stopping means the entire downstream occupancy set
materialises the tick the signal clears. The release is computed from the train
ahead — matched on **head and tail**, since occupancy is both and a 700 m rake
straddling a boundary resolves its head into the wrong resource, which made the
lookup miss for the whole straddle and silently fall back to the truncating path.

**A pair contends on every shared resource, not just the nearest.**
`_all_clashes`, not `_first_clash`. Returning on the first overlap hides a pair's
later contentions until the earlier one resolves, at which point the hidden one
enters its group's `min()` at whatever value it has silently counted down to — a
751-second discontinuity with no train having moved unusually.

**Loop windows are excluded from pairwise intersection.** Loop capacity is already
a `NoOverlap` in the CP model; raising it here gives the controller a conflict with
no dispatch action attached.

**Line speed comes from topology.** It was hardcoded at 130 while the two
*single-line* links — the ones that generate every headline conflict — run at 100
and 75.

---

## Optimization

**No scalar score.** Ranking is lexicographic over a 9-element priority-class cost
vector; a scalar cannot represent an ordinal comparison. The old formula produced
`OPT-1` at 1.00 and `OPT-2` at 0.00 *with less total delay* — both true, together
making the engine look broken while behaving correctly. Replaced by `rank` plus
trade-off text naming what each option saves and costs, which is the language a
controller argues in.

**The hold cap governs discretionary delay, not total delay.** On a 40 km single
line most of a train's wait is the block being physically occupied by the queue
ahead. That is queueing, not starvation. `delay[i] ≤ forced_s[i] + max_hold_s`,
where `forced_s` is computed per solve from the actual order. No scalar cap can
govern both a 40 km and a 5 km link; the constant was never wrong, the quantity it
was compared against was.

**Loop capacity is a constraint; a main-line stand is always available.**
`in_loop[i] + on_main[i] == stopped[i]`, with `NoOverlap` per `loop_id`. Simply
forbidding a shared loop makes a conflict infeasible whenever two trains must stop
and only one loop exists — and the relaxation path only loosens `max_hold`, so the
controller would get a CRITICAL alert with no advice at all. Modelling both stop
kinds keeps every conflict solvable and prices the main-line stand honestly.

**Every intervention is rendered.** `directives` accumulated all of them while the
action text rendered two, so a four-train conflict authorised something the card
did not mention. Every branch that appends a directive also appends an
intervention, making under-reporting structurally impossible. Durations are
included because without them two scenarios with the same holds in different order
are indistinguishable on screen.

**`STAND_ON_MAIN` exists as a directive kind.** Previously the solver could rank an
unexecutable action first and the deck armed an Approve button over it. A stand
reuses the hold fields with `hold_loop_id = None`; `_movement_authority` already
stops the train at the station and `occupied_resources` already keeps it on the
running line, so almost no new physics was needed.

**A stand on the running line serves a crossing, never an overtake.** To be
overtaken you must physically leave the running line — that is what a loop is for.
A same-direction stand deadlocks: the standing train blocks the train it is waiting
for. The simulator refuses the directive.

---

## Closed loop

**The engine tracks committed plans and labels them, rather than going quiet.**
Detection and solving continue every tick. Alerts carry `plan_state`: `OPEN` (a
decision to make), `IN_FORCE` (re-solved, still recommends the running plan), or
`DIVERGED` (re-solved, now recommends something else).

Suppressing a dispatched `conflict_id` is the tempting fix and it is wrong: a
third train entering, or the held train failing, must be re-evaluated. Labelling
gives that for free — `DIVERGED` is what makes re-evaluation visible instead of
indistinguishable from a stuck card.

Plans are scoped to the train set they were solved for (a third train joining is a
different decision), fingerprinted order-independently (so re-lettering OPT-1 and
OPT-2 is not a divergence), and expire by fleet observation or TTL.

**Dispatch is exactly-once at the sink.** `_already_in_force` refuses a duplicate
directive set. Not cosmetic: re-applying `HOLD_AT_LOOP` recomputes
`hold_expires_sim_s` from the current clock, so a second press silently extended a
hold by another full cap.

**Severity hysteresis is asymmetric.** Escalation is immediate and unconditional;
de-escalation requires clearing the band edge by a margin and holding for a dwell.
Flapping is by definition an oscillation, and an oscillation cannot complete
without a de-escalation, so gating one direction stops the flap without ever
delaying bad news. `plan_state` is part of the publish key, so a `DIVERGED`
transition never sits behind a severity cooldown.

**A hold order for a station the train has already passed is re-targeted, not
applied.** The diversion test had a lower bound and no upper bound, so a train 20
km past its holding station was flagged as berthed in a loop it had never reached
— it stopped claiming the single line, movement authority handed the section to
the next train, and three trains ended up standing in one token while the panel
read SECTION CLEAR.

**The countdown is anchored locally and scaled by rate.** `ConflictAlert` carries
no timestamp, and the prediction is in simulation seconds while the browser ticks
in wall time.

---

## Presentation

**`network_health_score` uses the solver's own delay curve.** It was
`count(delay ≤ 300s) / n × 100` — with five trains, only 0/20/40/60/80/100 were
attainable, `round(..., 1)` implied precision the metric could not have, and one
train crossing five minutes moved the headline 20 points. It is now the section's
position on the same piecewise-linear convex cost the solver minimises, normalised
so a 30-minute-down train scores zero. Headline KPI and objective function measure
delay identically.

**The speed bar reads against booked speed.** Against the permitted ceiling a
train running perfectly to book reads 75% and looks like it is dawdling.

**`contracts.ts` mirrors the Pydantic models one-for-one.** The file asserts this
in its header; it drifted twice.

---

## Known limitations

**Conflicts are solved independently of one another.** A train can appear in
several concurrent conflicts, and nothing checks whether the resulting directive
sets are mutually consistent — the same train can be held at two loops under
contradictory assumptions. The correct answer is a rolling-horizon model over all
trains and resources; the practical answer is to warn when an approval touches a
train already under an active directive.

**Precedence enumeration is factorial**, capped at five trains (120 solves). This
is a deliberate trade: each solve is one nameable dispatch decision the controller
can be offered, where a single model with order as a decision variable gives one
optimum and no alternatives.

**`forced_s` is order-dependent**, so leading with the slowest train raises
everyone else's cap.

**`STAND_ON_MAIN` passes the resource's entry station on one train's route**,
which is correct for a crossing and permissive for an overtake. Currently latent
because the same-direction guard blocks that path; `_movement_authority` is the
interlock behind it either way.

**Dominated scenarios can be offered.** `solutions[:max_scenarios]` has no
dominance test, so a second option can be worse for one train and better for none.

**Map labels collide when trains stack at a loop.** Held trains share coordinates
and each label renders at a fixed offset — concentrated exactly where a controller
is looking.

**`policy_exceeded` has never been exercised.** `max_hold_seconds` is hardcoded in
`optimiser_inputs`, so config changes to the cap never reached the solver and the
falsification test measured nothing.

**Projected contention times jitter by 30–40 s** against a 10 s tick. Modelling
exit speed honestly (`v² = u² + 2as`, rather than assuming every train reaches
target by the end of every resource) is physically correct but did not remove it,
so the cause is elsewhere. Invisible at the displayed resolution — the countdown
reads in minutes.

