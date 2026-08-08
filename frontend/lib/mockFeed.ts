import {
  ConflictAlert,
  DispatchRecommendation,
  RailwayEvent,
  SignalAspect,
  SimulationTick,
  TrainTelemetry,
  TrainType,
} from "./contracts";

/**
 * A stand-in for simulator/main.py, emitting the exact same contracts on the
 * same cadence (tick every 2s, telemetry every 2.5s per train). It exists so
 * frontend work is not blocked on Redis being up, and so the panel has
 * something to show if the demo laptop's backend dies on stage.
 *
 * It is NOT a physics model and should not grow into one. When the real
 * simulator emits TRAIN_TELEMETRY, delete the env flag and this file stays
 * unused.
 */

// NDLS -> AGC corridor, roughly. Delhi down to Agra via Palwal and Mathura.
const CORRIDOR: { lat: number; lng: number; id: string }[] = [
  { lat: 28.6431, lng: 77.2197, id: "NDLS" },
  { lat: 28.5100, lng: 77.2800, id: "FDB" },
  { lat: 28.1487, lng: 77.3320, id: "PWL" },
  { lat: 27.8974, lng: 77.4560, id: "KSV" },
  { lat: 27.4924, lng: 77.6737, id: "MTJ" },
  { lat: 27.2380, lng: 77.9500, id: "RKM" },
  { lat: 27.1591, lng: 78.0100, id: "AGC" },
];

interface MockTrain {
  id: string;
  name: string;
  type: TrainType;
  priority: number;
  maxSpeed: number;
  /** 0..1 along the corridor. */
  progress: number;
  speedKmh: number;
  delaySeconds: number;
  aspect: SignalAspect;
}

const FLEET: MockTrain[] = [
  { id: "12626", name: "Kerala Express", type: "EXPRESS", priority: 9.5, maxSpeed: 130, progress: 0.08, speedKmh: 112, delaySeconds: 420, aspect: "GREEN" },
  { id: "12002", name: "Shatabdi Express", type: "SUPERFAST", priority: 9.8, maxSpeed: 150, progress: 0.02, speedKmh: 138, delaySeconds: 0, aspect: "GREEN" },
  { id: "12280", name: "Taj Express", type: "EXPRESS", priority: 8.2, maxSpeed: 110, progress: 0.31, speedKmh: 96, delaySeconds: 180, aspect: "YELLOW" },
  { id: "54402", name: "Palwal Passenger", type: "PASSENGER", priority: 4.5, maxSpeed: 80, progress: 0.45, speedKmh: 62, delaySeconds: 900, aspect: "YELLOW" },
  { id: "40201", name: "BOXN Rake 402", type: "FREIGHT", priority: 2.1, maxSpeed: 60, progress: 0.38, speedKmh: 41, delaySeconds: 1500, aspect: "RED" },
  { id: "40388", name: "BCNA Rake 388", type: "FREIGHT", priority: 2.4, maxSpeed: 60, progress: 0.62, speedKmh: 48, delaySeconds: 240, aspect: "GREEN" },
  { id: "12622", name: "Tamil Nadu Express", type: "SUPERFAST", priority: 9.1, maxSpeed: 140, progress: 0.71, speedKmh: 124, delaySeconds: 60, aspect: "GREEN" },
  { id: "51904", name: "Mathura Shuttle", type: "PASSENGER", priority: 3.9, maxSpeed: 75, progress: 0.83, speedKmh: 58, delaySeconds: 300, aspect: "GREEN" },
];

function pointAt(progress: number) {
  const clamped = Math.min(0.9999, Math.max(0, progress));
  const span = 1 / (CORRIDOR.length - 1);
  const index = Math.floor(clamped / span);
  const t = (clamped - index * span) / span;
  const a = CORRIDOR[index];
  const b = CORRIDOR[index + 1];
  return {
    coordinates: { lat: a.lat + (b.lat - a.lat) * t, lng: a.lng + (b.lng - a.lng) * t },
    nextStation: b.id,
    blockIndex: index,
  };
}

function telemetryFor(train: MockTrain): TrainTelemetry {
  const { coordinates, nextStation, blockIndex } = pointAt(train.progress);
  return {
    event_type: "TRAIN_TELEMETRY",
    train_id: train.id,
    train_name: train.name,
    train_type: train.type,
    priority_weight: train.priority,
    current_section_id: "NDLS-AGC-04",
    current_block_id: `BLK-${100 + blockIndex}${String.fromCharCode(65 + (blockIndex % 3))}`,
    coordinates,
    speed_kmh: Math.round(train.speedKmh * 10) / 10,
    max_allowed_speed_kmh: train.maxSpeed,
    schedule_status: train.delaySeconds > 120 ? "DELAYED" : "ON_TIME",
    delay_seconds: Math.round(train.delaySeconds),
    next_station_id: nextStation,
    eta_next_station: Date.now() + 1000 * 60 * (2 + Math.random() * 12),
    signal_aspect: train.aspect,
  };
}

const MOCK_CONFLICT: ConflictAlert = {
  event_type: "CONFLICT_PREDICTED",
  conflict_id: "CONF-8902",
  severity: "CRITICAL",
  predicted_time_to_conflict_seconds: 360,
  location: { section_id: "NDLS-AGC-04", junction_id: "JNC-PALWAL-02", track_id: "TRK-DOWN-MAIN" },
  conflicting_train_ids: ["12626", "40201"],
  root_cause:
    "BOXN Rake 402 occupying the down main at 41 km/h. Kerala Express closing at 112 km/h with no intervening loop before Palwal.",
  estimated_cascading_impact_minutes: 45,
};

const MOCK_RECOMMENDATION: DispatchRecommendation = {
  event_type: "DISPATCH_RECOMMENDATION",
  conflict_id: "CONF-8902",
  scenarios: [
    {
      scenario_id: "OPT-1",
      action: "Hold BOXN Rake 402 at Palwal loop line",
      network_impact: "Kerala Express 12626 delayed 0 min. Rake 402 delayed 45 min. Section throughput unchanged.",
      score: 0.88,
    },
    {
      scenario_id: "OPT-2",
      action: "Regulate Kerala Express 12626 to 70 km/h behind the rake",
      network_impact: "Kerala Express 12626 delayed 15 min. Rake 402 delayed 0 min. Two downstream connections at risk.",
      score: 0.32,
    },
    {
      scenario_id: "OPT-3",
      action: "Divert BOXN Rake 402 to the up main under caution order",
      network_impact: "Kerala Express 12626 delayed 3 min. Rake 402 delayed 12 min. Requires point clearance at JNC-PALWAL-02.",
      score: 0.61,
    },
  ],
};

export function startMockFeed(emit: (event: RailwayEvent) => void): () => void {
  let tickId = 0;
  let conflictArmed = true;

  const tick = setInterval(() => {
    tickId += 1;

    const event: SimulationTick = {
      event_type: "SIMULATION_TICK",
      timestamp: Date.now(),
      tick_id: tickId,
      time_multiplier: 5,
      active_train_count: FLEET.length,
      network_health_score: Math.round((72 + Math.sin(tickId / 12) * 9) * 10) / 10,
    };
    emit(event);

    // Fire the scripted conflict once the fleet is on screen.
    if (conflictArmed && tickId === 6) {
      conflictArmed = false;
      emit(MOCK_CONFLICT);
      emit(MOCK_RECOMMENDATION);
    }
  }, 2000);

  const telemetry = setInterval(() => {
    for (const train of FLEET) {
      // Corridor is ~200km; time_multiplier 5 means 2.5s wall == 12.5s sim.
      const km = (train.speedKmh * 12.5) / 3600;
      train.progress = (train.progress + km / 200) % 1;

      train.speedKmh = Math.max(
        0,
        Math.min(train.maxSpeed, train.speedKmh + (Math.random() - 0.5) * 8),
      );
      train.delaySeconds = Math.max(0, train.delaySeconds + (Math.random() - 0.45) * 30);
      train.aspect =
        train.speedKmh < 15 ? "RED" : train.speedKmh < train.maxSpeed * 0.6 ? "YELLOW" : "GREEN";

      emit(telemetryFor(train));
    }
  }, 2500);

  // Prime the panel immediately rather than making the user wait 2.5s.
  for (const train of FLEET) emit(telemetryFor(train));

  return () => {
    clearInterval(tick);
    clearInterval(telemetry);
  };
}
