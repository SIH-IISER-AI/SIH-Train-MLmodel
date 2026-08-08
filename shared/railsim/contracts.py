from pydantic import BaseModel
from typing import Any, Dict, List


class SimulationTick(BaseModel):
    event_type: str = "SIMULATION_TICK"
    timestamp: int
    tick_id: int
    time_multiplier: int
    active_train_count: int
    network_health_score: float


class TrainTelemetry(BaseModel):
    event_type: str = "TRAIN_TELEMETRY"
    train_id: str
    train_name: str
    train_type: str
    priority_weight: float
    current_section_id: str
    current_block_id: str
    coordinates: Dict[str, float]
    speed_kmh: float
    max_allowed_speed_kmh: float
    schedule_status: str
    delay_seconds: int
    next_station_id: str
    eta_next_station: int
    signal_aspect: str
    route_progress_km: float
    resource_id: str
    track_id: str
    direction: str


class ConflictAlert(BaseModel):
    event_type: str = "CONFLICT_PREDICTED"
    conflict_id: str
    severity: str
    predicted_time_to_conflict_seconds: int
    location: Dict[str, str]
    conflicting_train_ids: List[str]
    root_cause: str
    estimated_cascading_impact_minutes: int


class Scenario(BaseModel):
    scenario_id: str
    action: str
    network_impact: str
    score: float
    directives: List[Dict[str, Any]] = []
    policy_exceeded: bool = False

class DispatchRecommendation(BaseModel):
    event_type: str = "DISPATCH_RECOMMENDATION"
    conflict_id: str
    scenarios: List[Scenario]


class ControllerAction(BaseModel):
    event_type: str = "CONTROLLER_ACTION"
    conflict_id: str
    scenario_id: str
    timestamp: int