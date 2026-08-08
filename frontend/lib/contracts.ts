/**
 * Wire contracts. These mirror simulator/contracts.py and ai-engine/contracts.py
 * one-for-one. If a field changes in Pydantic, it changes here in the same PR --
 * that is the whole point of having a single stream with an `event_type` tag.
 */

export type SignalAspect = "RED" | "YELLOW" | "DOUBLE_YELLOW" | "GREEN";
export type ScheduleStatus = "ON_TIME" | "DELAYED" | "EARLY" | "HELD";
export type TrainType = "EXPRESS" | "SUPERFAST" | "PASSENGER" | "FREIGHT" | "SPECIAL";
export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export interface Coordinates {
  lat: number;
  lng: number;
}

export interface SimulationTick {
  event_type: "SIMULATION_TICK";
  timestamp: number;
  tick_id: number;
  time_multiplier: number;
  active_train_count: number;
  network_health_score: number;
}

export interface TrainTelemetry {
  event_type: "TRAIN_TELEMETRY";
  train_id: string;
  train_name: string;
  train_type: TrainType;
  priority_weight: number;
  current_section_id: string;
  current_block_id: string;
  coordinates: Coordinates;
  speed_kmh: number;
  max_allowed_speed_kmh: number;
  schedule_status: ScheduleStatus;
  delay_seconds: number;
  next_station_id: string;
  eta_next_station: number;
  signal_aspect: SignalAspect;
}

export interface ConflictAlert {
  event_type: "CONFLICT_PREDICTED";
  conflict_id: string;
  severity: Severity;
  predicted_time_to_conflict_seconds: number;
  location: {
    section_id: string;
    junction_id: string;
    track_id: string;
  };
  conflicting_train_ids: string[];
  root_cause: string;
  estimated_cascading_impact_minutes: number;
}

export type DirectiveKind = "HOLD_AT_LOOP" | "REGULATE" | "RELEASE";

export interface Directive {
  kind: DirectiveKind;
  train_id: string;
  station_id?: string | null;
  loop_id?: string | null;
  until_train_id?: string | null;
  max_hold_seconds?: number;
  target_speed_kmh?: number;
}

export interface Scenario {
  scenario_id: string;
  action: string;
  network_impact: string;
  score: number;
  /** Machine-executable form. The simulator applies these all-or-nothing. */
  directives: Directive[];
  /** True when no ordering fit inside max_hold_seconds and the cap was relaxed. */
  policy_exceeded: boolean;
}

export interface DispatchRecommendation {
  event_type: "DISPATCH_RECOMMENDATION";
  conflict_id: string;
  scenarios: Scenario[];
}

/** Sent UI -> ws-server -> Redis -> simulator. The only upstream message. */
export interface ControllerAction {
  event_type: "CONTROLLER_ACTION";
  conflict_id: string;
  scenario_id: string;
  timestamp: number;
}

export type RailwayEvent =
  | SimulationTick
  | TrainTelemetry
  | ConflictAlert
  | DispatchRecommendation;

/**
 * Runtime guard. The socket hands us `any`; everything downstream of this
 * function is typed. Anything unrecognised is dropped loudly, not silently --
 * a contract drift between Python and TS should show up in the console during
 * the demo, not as an empty map.
 */
export function isRailwayEvent(value: unknown): value is RailwayEvent {
  if (typeof value !== "object" || value === null) return false;
  const t = (value as { event_type?: unknown }).event_type;
  return (
    t === "SIMULATION_TICK" ||
    t === "TRAIN_TELEMETRY" ||
    t === "CONFLICT_PREDICTED" ||
    t === "DISPATCH_RECOMMENDATION"
  );
}

// ---------------------------------------------------------------------------
// Derived helpers -- presentation logic that depends on contract semantics
// lives next to the contract, not scattered through components.
// ---------------------------------------------------------------------------

export function isFreight(train: TrainTelemetry): boolean {
  return train.train_type === "FREIGHT" || train.priority_weight < 4;
}

/** Delay in whole minutes, signed. Negative = running early. */
export function delayMinutes(train: TrainTelemetry): number {
  return Math.round(train.delay_seconds / 60);
}

/** How close a train is to its permitted ceiling. Used for the speed bar. */
export function speedRatio(train: TrainTelemetry): number {
  if (!train.max_allowed_speed_kmh) return 0;
  return Math.min(1, Math.max(0, train.speed_kmh / train.max_allowed_speed_kmh));
}

export function formatClock(epochMs: number): string {
  if (!Number.isFinite(epochMs) || epochMs <= 0) return "--:--:--";
  return new Date(epochMs).toLocaleTimeString("en-IN", { hour12: false });
}

export function formatCountdown(seconds: number): string {
  const clamped = Math.max(0, Math.round(seconds));
  const m = Math.floor(clamped / 60);
  const s = clamped % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}