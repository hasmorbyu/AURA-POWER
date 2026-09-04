"""API contracts. Both scenarios return the identical InterventionResult shape,
so the frontend needs exactly one rendering pipeline."""
from typing import Literal

from pydantic import BaseModel, Field


class CorridorState(BaseModel):
    from_: str = Field(alias="from")
    to: str
    capacity_mw: float
    flow_mw: float
    loading: float
    max_circuit_loading: float
    voltage_kv: float = 0.0
    circuits_live: int
    circuits_total: int

    model_config = {"populate_by_name": True}


class SubstationState(BaseModel):
    name: str
    lat: float
    lon: float
    role: Literal["source", "demand"]
    demand_mw: float
    served_mw: float
    shed_mw: float


class RiskSummary(BaseModel):
    max_corridor_loading: float
    max_circuit_loading: float
    circuits_at_limit: int
    corridors_at_risk: int
    at_risk_corridors: list[str]
    total_demand_mw: float
    total_served_mw: float
    total_shed_mw: float
    failed_lines: int
    switched_out_lines: int = 0


class RejectedAlternative(BaseModel):
    attempt: int
    reason: str
    violating_assets: list[dict]


class InterventionAction(BaseModel):
    kind: Literal["reroute", "shed", "switch_out", "switch_in"]
    corridor: str | None = None
    substation: str | None = None
    delta_mw: float
    detail: str


class InterventionResult(BaseModel):
    tick: int
    trigger: dict
    accepted: bool
    actions: list[InterventionAction]
    rejected_alternatives: list[RejectedAlternative]
    affected_corridors: list[str]
    affected_substations: list[str]
    utilisation_before: dict[str, float]
    utilisation_after: dict[str, float]
    mw_restored: float
    mw_rerouted: float
    estimated_loss_mw: float
    risk_before: RiskSummary
    risk_after: RiskSummary
    power_flow_validated: bool
    message: str


class NetworkState(BaseModel):
    tick: int
    substations: list[SubstationState]
    corridors: dict[str, CorridorState]
    future_corridors: list[dict]
    risk: RiskSummary
    demand_scale: float
    temperature_c: float | None


class TickRequest(BaseModel):
    as_of: str | None = None
    horizon_h: int = 2


class DisturbanceRequest(BaseModel):
    kind: Literal["temperature", "demand", "line_failure"]
    temperature_delta_c: float | None = None
    demand_multiplier: float | None = None
    corridor: str | None = None          # "A|B" -- fails every circuit on it
    line_gids: list[int] | None = None   # or specific lines
