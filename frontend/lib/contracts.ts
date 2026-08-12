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

export interface SystemReady {
  event_type: "SYSTEM_READY";
  epoch: string;
  timestamp: number;
  section_id: string;
  train_ids: string[];
  tick_seconds: number;
  time_multiplier: number;
}

export interface SimulationTick {
  event_type: "SIMULATION_TICK";
  timestamp: number;
  tick_id: number;
  time_multiplier: number;
  active_train_count: number;
  network_health_score: number;
  epoch?: string;
}

export type TrackDirection = "UP" | "DOWN";

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
  /** What the train is booked to run at. Not what the rake can do. */
  scheduled_speed_kmh: number;
  schedule_status: ScheduleStatus;
  delay_seconds: number;
  next_station_id: string;
  eta_next_station: number;
  signal_aspect: SignalAspect;
  /** Distance along this train's own route. The detector's projection basis. */
  route_progress_km: number;
  /** The unit of mutual exclusion. Compare THIS between trains, never block. */
  resource_id: string;
  track_id: string;
  direction: TrackDirection;
  /** Where the train is booked to stop. Set the moment a hold is accepted. */
  hold_station_id: string | null;
  hold_loop_id: string | null;
  /** Which loop the rake is STANDING in. Null while it is still on the main. */
  in_loop_id: string | null;
  standing_on_main: boolean;
  hold_until_train_id: string | null;
  hold_expires_in_s: number | null;
}

/**
 * OPEN      no accepted plan; the card is a decision to make.
 * IN_FORCE  the engine re-solved and still recommends the plan already running.
 * DIVERGED  the engine re-solved and now recommends something else. Loud.
 */
export type PlanState = "OPEN" | "IN_FORCE" | "DIVERGED";

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
  epoch?: string;
  plan_state?: PlanState;
  plan_in_force?: string | null;
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

export interface DelayBreakdown {
  train_id: string;
  train_name: string;
  delay_seconds: number;
  /** Delay the block's occupancy by trains ahead makes unavoidable. */
  queued_seconds: number;
  /** Delay above the queued floor -- the part the optimiser chose. */
  dispatch_choice_seconds: number;
}

export interface Scenario {
  scenario_id: string;
  /** 1 = leading scenario. Ordering is lexicographic over IR priority classes. */
  rank: number;
  action: string;
  /** Why it leads, or what it trades away against the leader. */
  rationale: string;
  network_impact: string;
  directives: Directive[];
  delay_breakdown: DelayBreakdown[];
  policy_exceeded: boolean;
}

export interface DispatchRecommendation {
  event_type: "DISPATCH_RECOMMENDATION";
  conflict_id: string;
  scenarios: Scenario[];
  epoch?: string;
}

/** Sent UI -> ws-server -> Redis -> simulator. The only upstream message. */
export interface ControllerAction {
  event_type: "CONTROLLER_ACTION";
  conflict_id: string;
  scenario_id: string;
  epoch?: string;
  timestamp: number;
}

export type ActionOutcome = "applied" | "no_op" | "rejected";

/**
 * The simulator's verdict on a CONTROLLER_ACTION, off control_stream.
 *
 * Without it the client clears a card optimistically and a rejection is
 * indistinguishable from a success. It also retires the conflict: the engine
 * never publishes a resolution, so backfill would otherwise resurrect every
 * conflict the controller has already dispatched.
 */
export interface ControllerActionResult {
  event_type: "CONTROLLER_ACTION_RESULT";
  conflict_id: string;
  scenario_id: string;
  epoch?: string;
  outcome: ActionOutcome;
  reason: string;
  directives_applied: number;
  timestamp: number;
}

export type RailwayEvent =
  | SystemReady
  | SimulationTick
  | TrainTelemetry
  | ConflictAlert
  | DispatchRecommendation
  | ControllerActionResult;

export function isRailwayEvent(value: unknown): value is RailwayEvent {
  if (typeof value !== "object" || value === null) return false;
  const t = (value as { event_type?: unknown }).event_type;
  return (
    t === "SYSTEM_READY" ||
    t === "SIMULATION_TICK" ||
    t === "TRAIN_TELEMETRY" ||
    t === "CONFLICT_PREDICTED" ||
    t === "DISPATCH_RECOMMENDATION" ||
    t === "CONTROLLER_ACTION_RESULT"
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
  const reference = train.scheduled_speed_kmh || train.max_allowed_speed_kmh;
  if (!reference) return 0;
  return Math.min(1, Math.max(0, train.speed_kmh / reference));
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