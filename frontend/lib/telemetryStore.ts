import {
  ActionOutcome,
  ConflictAlert,
  ControllerActionResult,
  Coordinates,
  DispatchRecommendation,
  PlanState,
  RailwayEvent,
  SimulationTick,
  SystemReady,
  TrainTelemetry,
} from "./contracts";

/**
 * WHY THIS FILE EXISTS
 * --------------------
 * Telemetry lands every 2-3s per train, for ~40 trains. If every packet called
 * setState, React would reconcile the whole dashboard several times a second for
 * data that mostly did not change. So the authoritative state is a plain Map
 * held outside React (the `trains` field, which the hook exposes as
 * `trainsRef.current`), and React is notified on a schedule instead of on
 * arrival.
 *
 * Three notification channels, because the three consumers have genuinely
 * different urgency:
 *
 *   trains    -> flushed at 10Hz. The roster and counters. Telemetry arrives
 *                slower than this, so nothing is lost, and re-renders are capped.
 *   clock     -> flushed on SIMULATION_TICK. Once every ~2s. Header only.
 *   conflicts -> flushed immediately. A CRITICAL alert must not wait 100ms
 *                behind a batching timer.
 *
 * The map subscribes to nothing. It runs its own requestAnimationFrame loop and
 * reads `store.trains` directly, so it can dead-reckon positions between packets
 * without involving React at all. See `positionAt()`.
 *
 * Everything here is scoped to one simulator epoch. A new epoch means the
 * positions, conflicts and recommendations held in this store describe a run
 * that no longer exists, so the store is flushed rather than reconciled.
 */

export type Channel = "trains" | "clock" | "conflicts";

const FLUSH_INTERVAL_MS = 100; // 10Hz ceiling on train-driven re-renders
const TRAIL_LENGTH = 48; // breadcrumbs kept per train for the track trace
const SNAP_DISTANCE_KM = 3; // beyond this, treat a fix as a teleport, don't ease
const STALE_AFTER_MS = 30_000; // no packet for 30s -> train is presumed lost
const ACTION_TIMEOUT_MS = 10_000; // no verdict in 10s -> stop blocking the card
const DISPATCH_LEDGER_TTL_MS = 30 * 60_000; // a committed plan is remembered this long
/** A live conflict is republished at least once per engine alert cooldown, so
 *  anything unseen for this long has stopped being detected. Without a sweep a
 *  long run accumulates one card per train-set that has ever contended. */
const CONFLICT_TTL_MS = 180_000;

export interface Fix {
  coordinates: Coordinates;
  /** performance.now() at which this fix was applied locally. */
  at: number;
  speedKmh: number;
  /** Degrees clockwise from north, derived from the previous fix. */
  headingDeg: number;
}

/** Telemetry plus the client-side motion state we layer on top of it. */
export interface TrackedTrain {
  telemetry: TrainTelemetry;
  fix: Fix;
  /** Smoothed screen-space truth, updated by the render loop, never by the socket. */
  rendered: Coordinates;
  trail: Coordinates[];
  updatedAt: number;
}

/** A dispatched action with no verdict yet. */
export interface PendingAction {
  scenarioId: string;
  at: number;
}

/** The last verdict for a conflict, for the card to surface. */
export interface ActionFeedback {
  conflictId: string;
  scenarioId: string;
  outcome: ActionOutcome;
  reason: string;
  at: number;
}

/** A plan the controller committed to and that has not been superseded. */
export interface DispatchedPlan {
  scenarioId: string;
  trainIds: string[];
  at: number;
}

const EARTH_KM_PER_DEG_LAT = 110.574;

function kmPerDegLng(lat: number): number {
  return 111.32 * Math.cos((lat * Math.PI) / 180);
}

export function haversineKm(a: Coordinates, b: Coordinates): number {
  const dLat = (b.lat - a.lat) * EARTH_KM_PER_DEG_LAT;
  const dLng = (b.lng - a.lng) * kmPerDegLng((a.lat + b.lat) / 2);
  return Math.hypot(dLat, dLng);
}

function bearingDeg(from: Coordinates, to: Coordinates): number {
  const y = (to.lng - from.lng) * kmPerDegLng((from.lat + to.lat) / 2);
  const x = (to.lat - from.lat) * EARTH_KM_PER_DEG_LAT;
  if (x === 0 && y === 0) return 0;
  return (Math.atan2(y, x) * 180) / Math.PI;
}

function project(origin: Coordinates, headingDeg: number, km: number): Coordinates {
  const rad = (headingDeg * Math.PI) / 180;
  return {
    lat: origin.lat + (Math.cos(rad) * km) / EARTH_KM_PER_DEG_LAT,
    lng: origin.lng + (Math.sin(rad) * km) / kmPerDegLng(origin.lat),
  };
}

export class TelemetryStore {
  /** Authoritative train state. Exposed to the hook as trainsRef.current. */
  readonly trains = new Map<string, TrackedTrain>();
  readonly conflicts = new Map<string, ConflictAlert>();
  readonly recommendations = new Map<string, DispatchRecommendation>();

  /** Conflicts awaiting a simulator verdict, keyed by conflict_id. */
  readonly pendingActions = new Map<string, PendingAction>();

  /**
   * Plans the controller has already committed to, keyed by conflict_id.
   *
   * This is the ledger that survives re-publication. The engine keeps
   * detecting and keeps solving -- correctly -- so the same conflict_id comes
   * back on the wire after it has been dispatched. Without a record of what was
   * already sent, that republish is indistinguishable from a new decision and
   * the controller presses Approve a second time.
   */
  readonly dispatched = new Map<string, DispatchedPlan>();

  /**
   * Conflicts the engine has re-raised while their accepted plan is still the
   * plan it would recommend. Rendered as a muted in-force strip, never as an
   * armed card. Kept out of `conflicts` so the deck's count and ordering
   * reflect open decisions only.
   */
  readonly acknowledged = new Map<string, ConflictAlert>();

  /** Most recent verdict. Cleared when the same conflict is dispatched again. */
  lastFeedback: ActionFeedback | null = null;

  clock: SimulationTick | null = null;
  epoch: string | null = null;
  ready: SystemReady | null = null;
  lastEventAt = 0;
  droppedEvents = 0;

  /** Publish-time anchor per conflict. predicted_time_to_conflict_seconds is a
   *  snapshot carrying no timestamp, so every surface that renders a countdown
   *  must decrement it from the same instant or they contradict each other. */
  private countdownAnchors = new Map<string, { seconds: number; at: number }>();
  private lastSeenConflict = new Map<string, number>();

  secondsToConflict(conflict: ConflictAlert): number {
    const anchor = this.countdownAnchors.get(conflict.conflict_id);
    if (!anchor) return Math.max(0, conflict.predicted_time_to_conflict_seconds);
    const rate = this.clock?.time_multiplier ?? 1;
    return Math.max(
      0,
      Math.round(anchor.seconds - ((Date.now() - anchor.at) / 1000) * rate),
    );
  }

  private versions: Record<Channel, number> = { trains: 0, clock: 0, conflicts: 0 };
  private listeners: Record<Channel, Set<() => void>> = {
    trains: new Set(),
    clock: new Set(),
    conflicts: new Set(),
  };
  private dirty: Record<Channel, boolean> = {
    trains: false,
    clock: false,
    conflicts: false,
  };
  private flushTimer: ReturnType<typeof setInterval> | null = null;

  // -- subscription surface (shaped for useSyncExternalStore) ----------------

  subscribe = (channel: Channel) => (onStoreChange: () => void) => {
    this.listeners[channel].add(onStoreChange);
    this.ensureFlushLoop();
    return () => {
      this.listeners[channel].delete(onStoreChange);
    };
  };

  getVersion = (channel: Channel) => () => this.versions[channel];

  /** SSR has no socket, so the server snapshot is always version 0. */
  getServerVersion = () => 0;

  private ensureFlushLoop() {
    if (this.flushTimer !== null) return;
    this.flushTimer = setInterval(() => this.flush("trains"), FLUSH_INTERVAL_MS);
  }

  private flush(channel: Channel) {
    if (!this.dirty[channel]) return;
    this.dirty[channel] = false;
    this.versions[channel] += 1;
    for (const listener of this.listeners[channel]) listener();
  }

  private markDirty(channel: Channel, immediate = false) {
    this.dirty[channel] = true;
    if (immediate) this.flush(channel);
  }

  // -- ingest ----------------------------------------------------------------

  /** Single entry point for everything coming off the socket. */
  ingest(event: RailwayEvent) {
    this.lastEventAt = Date.now();

    switch (event.event_type) {
      case "SYSTEM_READY":
        this.adoptEpoch(event.epoch);
        this.ready = event;
        break;

      case "TRAIN_TELEMETRY":
        this.applyTelemetry(event);
        this.markDirty("trains");
        break;

      case "SIMULATION_TICK":
        if (event.epoch) this.adoptEpoch(event.epoch);
        this.clock = event;
        this.reapStaleTrains();
        this.reapPendingActions();
        this.markDirty("clock", true);
        break;

      case "CONFLICT_PREDICTED":
        if (this.isForeignEpoch(event.epoch)) return;
        this.applyConflict(event);
        this.markDirty("conflicts", true);
        break;

      case "DISPATCH_RECOMMENDATION":
        if (this.isForeignEpoch(event.epoch)) return;
        this.recommendations.set(event.conflict_id, event);
        this.markDirty("conflicts", true);
        break;

      case "CONTROLLER_ACTION_RESULT":
        if (this.isForeignEpoch(event.epoch)) return;
        this.applyActionResult(event);
        this.markDirty("conflicts", true);
        break;
    }
  }

  private sameTrainSet(a: string[], b: string[]): boolean {
    if (a.length !== b.length) return false;
    const left = [...a].sort();
    const right = [...b].sort();
    return left.every((id, index) => id === right[index]);
  }

  /**
   * Decide whether an incoming alert is an open decision or the acknowledgement
   * of one already made.
   *
   * Three ways a ledger entry stops applying, and all three re-arm the card:
   *   - the engine says DIVERGED: it re-solved and now recommends something
   *     else, which is the most urgent thing it can tell a controller
   *   - the train set changed: a third train joining is a different decision
   *   - the ledger entry aged out
   *
   * OPEN with a ledger entry present is treated as in force. That is the case
   * where the engine has not yet seen the action verdict, and the simulator's
   * exactly-once guard already makes a second press a no-op -- so the honest
   * render is "in force", not a second armed button.
   */
  private applyConflict(event: ConflictAlert) {
    const anchor = this.countdownAnchors.get(event.conflict_id);
    if (!anchor || anchor.seconds !== event.predicted_time_to_conflict_seconds) {
      this.countdownAnchors.set(event.conflict_id, {
        seconds: event.predicted_time_to_conflict_seconds,
        at: Date.now(),
      });
    }
    this.lastSeenConflict.set(event.conflict_id, Date.now());
    
    const plan = this.dispatched.get(event.conflict_id);
    const state: PlanState = event.plan_state ?? "OPEN";

    if (plan && Date.now() - plan.at > DISPATCH_LEDGER_TTL_MS) {
      this.dispatched.delete(event.conflict_id);
    } else if (plan && !this.sameTrainSet(plan.trainIds, event.conflicting_train_ids)) {
      this.dispatched.delete(event.conflict_id);
    } else if (plan && state === "DIVERGED") {
      this.dispatched.delete(event.conflict_id);
    } else if (plan) {
      this.acknowledged.set(event.conflict_id, event);
      this.conflicts.delete(event.conflict_id);
      return;
    }

    this.acknowledged.delete(event.conflict_id);
    this.conflicts.set(event.conflict_id, event);
  }

  /**
   * A changed epoch means every position, conflict and recommendation held here
   * belongs to a simulator run that no longer exists. Flush, then adopt.
   */
  private adoptEpoch(epoch: string) {
    if (this.epoch === epoch) return;
    const previous = this.epoch;
    this.reset();
    this.epoch = epoch;
    if (previous !== null) {
      console.info(`[telemetry] epoch ${previous} -> ${epoch}, store flushed`);
    }
  }

  private isForeignEpoch(epoch: string | undefined): boolean {
    return Boolean(epoch && this.epoch && epoch !== this.epoch);
  }

  private applyTelemetry(t: TrainTelemetry) {
    const now = performance.now();
    const existing = this.trains.get(t.train_id);

    if (!existing) {
      this.trains.set(t.train_id, {
        telemetry: t,
        fix: {
          coordinates: t.coordinates,
          at: now,
          speedKmh: t.speed_kmh,
          // No previous fix, so no heading yet. It resolves on packet two.
          headingDeg: 0,
        },
        rendered: { ...t.coordinates },
        trail: [t.coordinates],
        updatedAt: now,
      });
      return;
    }

    const moved = haversineKm(existing.fix.coordinates, t.coordinates);
    const heading =
      moved > 0.01
        ? bearingDeg(existing.fix.coordinates, t.coordinates)
        : existing.fix.headingDeg;

    existing.telemetry = t;
    existing.fix = {
      coordinates: t.coordinates,
      at: now,
      speedKmh: t.speed_kmh,
      headingDeg: heading,
    };
    existing.updatedAt = now;

    // A jump larger than SNAP_DISTANCE_KM is a re-spawn or a topology hop,
    // not motion. Cut the trail so we don't draw a line across the map.
    if (moved > SNAP_DISTANCE_KM) {
      existing.rendered = { ...t.coordinates };
      existing.trail = [t.coordinates];
    } else {
      existing.trail.push(t.coordinates);
      if (existing.trail.length > TRAIL_LENGTH) existing.trail.shift();
    }
  }

  /** Drop trains that have gone quiet. Otherwise ghosts accumulate on the panel. */
  private reapStaleTrains() {
    const cutoff = performance.now() - STALE_AFTER_MS;
    let removed = false;
    for (const [id, train] of this.trains) {
      if (train.updatedAt < cutoff) {
        this.trains.delete(id);
        removed = true;
      }
    }
    if (removed) this.markDirty("trains", true);
  }
  
  private sweepStaleConflicts() {
    const cutoff = Date.now() - CONFLICT_TTL_MS;
    for (const [id, seen] of this.lastSeenConflict) {
      if (seen >= cutoff) continue;
      this.lastSeenConflict.delete(id);
      this.countdownAnchors.delete(id);
      this.conflicts.delete(id);
      this.acknowledged.delete(id);
      this.recommendations.delete(id);
    }
  }

  // -- controller actions ----------------------------------------------------

  /**
   * Called on dispatch. The card is deliberately NOT cleared here -- it stays,
   * disabled, until the simulator rules on it, so a rejection is visible instead
   * of looking identical to a success.
   */
  markPending(conflictId: string, scenarioId: string) {
    this.pendingActions.set(conflictId, { scenarioId, at: Date.now() });
    if (this.lastFeedback?.conflictId === conflictId) this.lastFeedback = null;
    this.markDirty("conflicts", true);
  }

  private applyActionResult(event: ControllerActionResult) {
    this.pendingActions.delete(event.conflict_id);
    this.lastFeedback = {
      conflictId: event.conflict_id,
      scenarioId: event.scenario_id,
      outcome: event.outcome,
      reason: event.reason,
      at: Date.now(),
    };
    // "applied" and "no_op" both mean the controller's decision stands, so the
    // conflict retires HERE rather than optimistically at click time. This is
    // also what stops backfill resurrecting a dispatched conflict on refresh:
    // the result is replayed from control_stream alongside the stale alert.
    if (event.outcome === "applied" || event.outcome === "no_op") {
      const alert =
        this.conflicts.get(event.conflict_id) ??
        this.acknowledged.get(event.conflict_id);
      this.dispatched.set(event.conflict_id, {
        scenarioId: event.scenario_id,
        trainIds: [...(alert?.conflicting_train_ids ?? [])],
        at: Date.now(),
      });
      this.conflicts.delete(event.conflict_id);
      this.acknowledged.delete(event.conflict_id);
      this.recommendations.delete(event.conflict_id);
    }
  }

  /** A verdict that never arrives must not lock a card forever. */
  private reapPendingActions() {
    const cutoff = Date.now() - ACTION_TIMEOUT_MS;
    for (const [conflictId, pending] of this.pendingActions) {
      if (pending.at < cutoff) {
        this.pendingActions.delete(conflictId);
        this.lastFeedback = {
          conflictId,
          scenarioId: pending.scenarioId,
          outcome: "rejected",
          reason: "no response from simulator",
          at: Date.now(),
        };
        this.markDirty("conflicts", true);
      }
    }
  }

  // -- read side -------------------------------------------------------------

  /**
   * Where a train is *right now*, extrapolated forward from its last fix along
   * its heading at its last reported speed. Called once per animation frame per
   * train by the panel. This is what turns a 2-3s packet rate into continuous
   * motion instead of a slideshow.
   *
   * `rendered` is then eased toward this prediction rather than assigned, so
   * that a correcting packet nudges the marker instead of snapping it.
   */
  positionAt(train: TrackedTrain, now: number, frameDeltaMs: number): Coordinates {
    const elapsedS = Math.max(0, (now - train.fix.at) / 1000);
    const km = (train.fix.speedKmh * elapsedS) / 3600;
    const predicted =
      km > 0 ? project(train.fix.coordinates, train.fix.headingDeg, km) : train.fix.coordinates;

    // Exponential smoothing, frame-rate independent. TAU 180ms: a correction is
    // ~95% applied within half a second, which reads as a nudge, not a jump.
    const alpha = 1 - Math.exp(-frameDeltaMs / 180);
    train.rendered = {
      lat: train.rendered.lat + (predicted.lat - train.rendered.lat) * alpha,
      lng: train.rendered.lng + (predicted.lng - train.rendered.lng) * alpha,
    };
    return train.rendered;
  }

  /** Trains ordered the way a controller triages: worst problem first. */
  roster(): TrackedTrain[] {
    return [...this.trains.values()].sort((a, b) => {
      const conflictA = this.isInConflict(a.telemetry.train_id) ? 1 : 0;
      const conflictB = this.isInConflict(b.telemetry.train_id) ? 1 : 0;
      if (conflictA !== conflictB) return conflictB - conflictA;
      if (a.telemetry.delay_seconds !== b.telemetry.delay_seconds) {
        return b.telemetry.delay_seconds - a.telemetry.delay_seconds;
      }
      return b.telemetry.priority_weight - a.telemetry.priority_weight;
    });
  }

  isInConflict(trainId: string): boolean {
    for (const conflict of this.conflicts.values()) {
      if (conflict.conflicting_train_ids.includes(trainId)) return true;
    }
    return false;
  }

  /** Open conflicts, most imminent first. */
  openConflicts(): ConflictAlert[] {
    this.sweepStaleConflicts();
    return [...this.conflicts.values()].sort(
      (a, b) => a.predicted_time_to_conflict_seconds - b.predicted_time_to_conflict_seconds,
    );
  }

  /**
   * Conflicts still live in the projection but covered by a plan the controller
   * already committed to. The engine has not gone quiet about them -- it is
   * still solving them every tick -- so the deck shows them, without a button.
   */
  acknowledgedConflicts(): ConflictAlert[] {
    this.sweepStaleConflicts();
    return [...this.acknowledged.values()].sort(
      (a, b) => a.predicted_time_to_conflict_seconds - b.predicted_time_to_conflict_seconds,
    );
  }

  planFor(conflictId: string): DispatchedPlan | undefined {
    return this.dispatched.get(conflictId);
  }

  /**
   * Force-clear a conflict card. NOT called on dispatch any more -- the
   * simulator's verdict does that via applyActionResult. Kept for the case
   * where something outside the action path needs to retire a card.
   */
  resolveConflict(conflictId: string) {
    this.conflicts.delete(conflictId);
    this.acknowledged.delete(conflictId);
    this.recommendations.delete(conflictId);
    this.pendingActions.delete(conflictId);
    this.markDirty("conflicts", true);
  }

  /** Aggregate delay across the section, in minutes. The headline number. */
  totalDelayMinutes(): number {
    let seconds = 0;
    for (const train of this.trains.values()) seconds += Math.max(0, train.telemetry.delay_seconds);
    return Math.round(seconds / 60);
  }

  reset() {
    this.trains.clear();
    this.conflicts.clear();
    this.acknowledged.clear();
    this.recommendations.clear();
    this.pendingActions.clear();
    this.dispatched.clear();
    this.lastFeedback = null;
    this.clock = null;
    this.epoch = null;
    this.ready = null;
    this.lastSeenConflict.clear();
    this.countdownAnchors.clear();
    this.markDirty("trains", true);
    this.markDirty("conflicts", true);
    this.markDirty("clock", true);
  }

  dispose() {
    if (this.flushTimer !== null) clearInterval(this.flushTimer);
    this.flushTimer = null;
  }
}