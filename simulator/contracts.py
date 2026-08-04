from pydantic import BaseModel
from typing import Dict, Any

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