from pydantic import BaseModel
from typing import List, Dict

# ... (Include SimulationTick and TrainTelemetry here as well) ...

class Scenario(BaseModel):
    scenario_id: str
    action: str
    network_impact: str
    score: float

class DispatchRecommendation(BaseModel):
    event_type: str = "DISPATCH_RECOMMENDATION"
    conflict_id: str
    scenarios: List[Scenario]

class ConflictAlert(BaseModel):
    event_type: str = "CONFLICT_PREDICTED"
    conflict_id: str
    severity: str
    predicted_time_to_conflict_seconds: int
    location: Dict[str, str]
    conflicting_train_ids: List[str]
    root_cause: str
    estimated_cascading_impact_minutes: int