from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class SimulationTick(BaseModel):
    event_type: str = "SIMULATION_TICK"
    timestamp: int
    tick_id: int
    time_multiplier: int
    active_train_count: int
    network_health_score: float
    epoch: str = ""

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
    scheduled_speed_kmh: float = 0.0
    schedule_status: str
    delay_seconds: int
    next_station_id: str
    eta_next_station: int
    signal_aspect: str
    route_progress_km: float
    resource_id: str
    track_id: str
    direction: str
    hold_station_id: Optional[str] = None
    hold_loop_id: Optional[str] = None
    in_loop_id: Optional[str] = None
    standing_on_main: bool = False
    hold_until_train_id: Optional[str] = None
    hold_expires_in_s: Optional[float] = None


class ConflictAlert(BaseModel):
    event_type: str = "CONFLICT_PREDICTED"
    conflict_id: str
    epoch: str = ""
    severity: str
    predicted_time_to_conflict_seconds: int
    location: Dict[str, str]
    conflicting_train_ids: List[str]
    root_cause: str
    estimated_cascading_impact_minutes: int
    plan_state: str = "OPEN"
    plan_in_force: Optional[str] = None


class Scenario(BaseModel):
    scenario_id: str
    rank: int = 1
    action: str
    rationale: str = ""
    network_impact: str
    directives: List[Dict[str, Any]] = []
    delay_breakdown: List[Dict[str, Any]] = []
    policy_exceeded: bool = False

class DispatchRecommendation(BaseModel):
    event_type: str = "DISPATCH_RECOMMENDATION"
    conflict_id: str
    epoch: str = ""
    scenarios: List[Scenario]


class ControllerAction(BaseModel):
    event_type: str = "CONTROLLER_ACTION"
    conflict_id: str
    scenario_id: str
    epoch: str = ""
    timestamp: int

class SystemReady(BaseModel):
    event_type: str = "SYSTEM_READY"
    epoch: str
    timestamp: int
    section_id: str
    train_ids: List[str]
    tick_seconds: float
    time_multiplier: int

class ControllerActionResult(BaseModel):
    """The simulator's verdict on a CONTROLLER_ACTION.

    Without this the client clears a card optimistically and a rejected action
    is indistinguishable from an applied one. It also retires the conflict: the
    engine never publishes a resolution, so backfill would otherwise resurrect
    every conflict the controller has already dispatched.
    """
    event_type: str = "CONTROLLER_ACTION_RESULT"
    conflict_id: str
    scenario_id: str
    epoch: str = ""
    outcome: str            # "applied" | "no_op" | "rejected"
    reason: str = ""
    directives_applied: int = 0
    timestamp: int