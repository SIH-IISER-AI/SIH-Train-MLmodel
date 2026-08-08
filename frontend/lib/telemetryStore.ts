import {
  ConflictAlert,
  Coordinates,
  DispatchRecommendation,
  RailwayEvent,
  SimulationTick,
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
 */

export type Channel = "trains" | "clock" | "conflicts";

const FLUSH_INTERVAL_MS = 100; // 10Hz ceiling on train-driven re-renders
const TRAIL_LENGTH = 48; // breadcrumbs kept per train for the track trace
const SNAP_DISTANCE_KM = 3; // beyond this, treat a fix as a teleport, don't ease
const STALE_AFTER_MS = 30_000; // no packet for 30s -> train is presumed lost

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

  clock: SimulationTick | null = null;
  lastEventAt = 0;
  droppedEvents = 0;

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
      case "TRAIN_TELEMETRY":
        this.applyTelemetry(event);
        this.markDirty("trains");
        break;

      case "SIMULATION_TICK":
        this.clock = event;
        this.reapStaleTrains();
        this.markDirty("clock", true);
        break;

      case "CONFLICT_PREDICTED":
        this.conflicts.set(event.conflict_id, event);
        this.markDirty("conflicts", true);
        break;

      case "DISPATCH_RECOMMENDATION":
        this.recommendations.set(event.conflict_id, event);
        this.markDirty("conflicts", true);
        break;
    }
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
    return [...this.conflicts.values()].sort(
      (a, b) => a.predicted_time_to_conflict_seconds - b.predicted_time_to_conflict_seconds,
    );
  }

  /** Called when the controller commits to a scenario -- clears the card. */
  resolveConflict(conflictId: string) {
    this.conflicts.delete(conflictId);
    this.recommendations.delete(conflictId);
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
    this.recommendations.clear();
    this.clock = null;
    this.markDirty("trains", true);
    this.markDirty("conflicts", true);
    this.markDirty("clock", true);
  }

  dispose() {
    if (this.flushTimer !== null) clearInterval(this.flushTimer);
    this.flushTimer = null;
  }
}
