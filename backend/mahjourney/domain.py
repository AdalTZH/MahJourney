from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Coordinate(FrozenModel):
    lat: float = Field(ge=1.15, le=1.5)
    lon: float = Field(ge=103.55, le=104.1)


class Vehicle(FrozenModel):
    vehicle_id: str
    driver_id: str
    capacity: int = Field(gt=0)
    start: Coordinate


class Order(FrozenModel):
    order_id: str
    address: str
    location: Coordinate
    demand: int = Field(gt=0)
    service_seconds: int = 300
    window_start_minute: int = 480
    window_end_minute: int = 1080
    cargo_tags: tuple[str, ...] = ()


class RouteStop(FrozenModel):
    stop_id: str
    sequence: int
    location: Coordinate
    eta_minute: int
    departure_minute: int
    demand: int


class VehicleRoute(FrozenModel):
    vehicle_id: str
    driver_id: str
    stops: tuple[RouteStop, ...]
    distance_km: float
    duration_minutes: int
    geometry: tuple[Coordinate, ...] = ()


class PlanVersion(FrozenModel):
    plan_id: str
    version: int
    status: Literal["CANDIDATE", "VALIDATED", "ACTIVE", "SUPERSEDED"]
    created_at: datetime = Field(default_factory=utc_now)
    source_data_version: str
    routes: tuple[VehicleRoute, ...]
    objective_cost: float
    hard_violations: tuple[str, ...] = ()


class PlanDelta(FrozenModel):
    from_version: int
    to_version: int
    changed_vehicles: tuple[str, ...]
    moved_stops: tuple[str, ...]
    cost_change: float


class SnapshotStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FETCH_FAILED = "FETCH_FAILED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class IntegrationHealth(FrozenModel):
    integration: str
    status: SnapshotStatus
    checked_at: datetime = Field(default_factory=utc_now)
    latency_ms: int | None = None
    message: str = ""
    expires_at: datetime | None = None


class TrafficSnapshot(FrozenModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    dataset: str
    fetched_at: datetime = Field(default_factory=utc_now)
    response_hash: str
    record_count: int
    status: SnapshotStatus
    records: tuple[dict[str, Any], ...] = ()


class WeatherSnapshot(FrozenModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    dataset: str
    fetched_at: datetime = Field(default_factory=utc_now)
    issue_timestamp: datetime | None = None
    response_hash: str
    status: SnapshotStatus
    records: tuple[dict[str, Any], ...] = ()


class RouteWeatherFeature(FrozenModel):
    route_leg_id: str
    rainfall_station_id: str | None
    forecast_area: str | None
    wet_or_dry: Literal["WET", "DRY", "UNKNOWN"]
    rain_expected: bool
    source_snapshot_ids: tuple[str, ...]


class DisruptionType(StrEnum):
    ROAD_CLOSURE = "ROAD_CLOSURE"
    URGENT_ORDER = "URGENT_ORDER"
    TRUCK_BREAKDOWN = "TRUCK_BREAKDOWN"
    HEAVY_RAIN = "HEAVY_RAIN"


class DisruptionEvent(FrozenModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    scenario_id: str
    event_type: DisruptionType
    effective_minute: int
    payload: dict[str, Any] = Field(default_factory=dict)


class SimulationClock(BaseModel):
    scenario_id: str = "demo"
    mode: Literal["LIVE", "SCENARIO"] = "SCENARIO"
    current_minute: int = 480
    playing: bool = False
    speed: Literal[1, 5, 20] = 1
    branch_parent_id: str | None = None


class PolicyTier(StrEnum):
    AUTO_EXECUTE = "AUTO_EXECUTE"
    NOTIFY_THEN_EXECUTE = "NOTIFY_THEN_EXECUTE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"


class PolicyInput(FrozenModel):
    same_driver: bool
    same_vehicle: bool
    protected_cargo: bool = False
    hard_violation: bool = False
    overtime: bool = False
    eta_degradation_minutes: float = 0
    final_stop_removed: bool = False
    hard_window_relaxed: bool = False
    data_stale: bool = False
    feasible: bool = True


class PolicyDecision(FrozenModel):
    tier: PolicyTier
    reasons: tuple[str, ...]
    veto_seconds: int = 0


class ApprovalRequest(FrozenModel):
    approval_id: str = Field(default_factory=lambda: str(uuid4()))
    plan_id: str
    plan_version: int
    action_digest: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXECUTED"] = "PENDING"
    created_at: datetime = Field(default_factory=utc_now)


class MemoryItem(FrozenModel):
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["POLICY", "PREFERENCE", "INCIDENT_LESSON"]
    content: str
    status: Literal["PROPOSED", "CURATED", "SUPERSEDED"] = "PROPOSED"
    trust_label: Literal["HUMAN_APPROVED", "UNTRUSTED_EXTERNAL"] = "UNTRUSTED_EXTERNAL"
    supersedes_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ConversationMessage(FrozenModel):
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    role: Literal["USER", "ASSISTANT"]
    content: str
    trust_label: str
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime


class AuditEvent(FrozenModel):
    sequence: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    timestamp: datetime = Field(default_factory=utc_now)
    previous_hash: str
    event_hash: str


class AgentTask(FrozenModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    task_type: str
    requester: str
    conversation_id: str
    plan_id: str | None = None
    plan_version: int | None = None
    input_references: tuple[str, ...] = ()
    constraints: dict[str, Any] = Field(default_factory=dict)
    trust_labels: tuple[str, ...] = ()
    deadline: datetime | None = None


class AgentResult(FrozenModel):
    task_id: str
    status: Literal["COMPLETED", "NEEDS_INPUT", "FAILED", "ESCALATED"]
    evidence_references: tuple[str, ...] = ()
    computed_metrics: dict[str, float | int | str | bool] = Field(default_factory=dict)
    proposed_actions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    escalation_reason: str | None = None
