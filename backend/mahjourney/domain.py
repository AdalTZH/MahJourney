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


class Depot(FrozenModel):
    depot_id: str
    name: str = ""
    location: Coordinate
    delivery_area: str = ""
    operating_start_minute: int = 0
    operating_end_minute: int = 1439
    cold_storage: bool = False
    status: str = "Operational"


class Driver(FrozenModel):
    driver_id: str
    name: str = ""
    depot_id: str = ""
    license_type: str = ""
    vocational_license: str = ""
    certification_type: str = ""
    working_start_minute: int = 480
    working_end_minute: int = 1080
    shift_type: str = ""
    skill_set: tuple[str, ...] = ()
    availability_status: str = "Available"


class Vehicle(FrozenModel):
    vehicle_id: str
    driver_id: str
    # ``capacity`` is retained as an abstract stop/demand ceiling for backward
    # compatibility with the synthetic fixtures and existing tests. Realistic
    # planning uses the weight/volume dimensions below.
    capacity: int = Field(default=25, gt=0)
    start: Coordinate
    depot_id: str = ""
    capacity_weight_kg: float = Field(default=1_000_000.0, gt=0)
    capacity_volume_m3: float = Field(default=1_000_000.0, gt=0)
    vehicle_type: str = ""
    license_plate: str = ""
    lta_vehicle_class: str = ""
    fuel_type: str = ""
    refrigerated: bool = False
    availability_start_minute: int = 0
    availability_end_minute: int = 1439
    availability_status: str = "Available"
    # Working window narrowed by the assigned driver's shift; defaults to the
    # vehicle's own availability window when no driver constraint applies.
    working_start_minute: int = 480
    working_end_minute: int = 1080


class Order(FrozenModel):
    order_id: str
    address: str
    postal_code: str = ""
    location: Coordinate
    demand: int = Field(default=1, gt=0)
    service_seconds: int = 300
    window_start_minute: int = 480
    window_end_minute: int = 1080
    cargo_tags: tuple[str, ...] = ()
    weight_kg: float = Field(default=1.0, ge=0)
    volume_m3: float = Field(default=0.0, ge=0)
    quantity: int = Field(default=1, ge=1)
    delivery_area: str = ""
    special_handling: str = "None"
    priority_level: int = 3
    customer_name: str = ""
    contact_phone: str = ""
    assigned_depot_id: str = ""
    assignment_note: str = ""


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
    computed_metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    proposed_actions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    escalation_reason: str | None = None


# ---------------------------------------------------------------------------
# Master-agent explicit state & memory management
# ---------------------------------------------------------------------------


class TurnPhase(StrEnum):
    """Explicit lifecycle phases for a single dispatcher turn.

    The master agent transitions through these phases in order. Having an
    explicit enum means every node can read and assert the current phase, and
    the API layer can surface a machine-readable phase to the UI without
    parsing free-text status strings.

    IDLE          No in-flight turn.
    ROUTING       Supervisor is deciding which workers to call.
    WORKER_RUN    A worker node is actively executing.
    SYNTHESIZING  Synthesize/respond node is composing the final reply.
    DONE          Turn complete; result available.
    ERROR         Unrecoverable error during the turn.
    """

    IDLE = "IDLE"
    ROUTING = "ROUTING"
    WORKER_RUN = "WORKER_RUN"
    SYNTHESIZING = "SYNTHESIZING"
    DONE = "DONE"
    ERROR = "ERROR"


class StateTransition(FrozenModel):
    """An immutable record of one phase change within a turn.

    Accumulated on GraphState so every turn carries a complete audit trail of
    what the master agent decided and when, without touching the external
    AuditChain (which is reserved for operator-visible events).
    """

    from_phase: TurnPhase
    to_phase: TurnPhase
    node: str
    reason: str = ""
    timestamp: datetime = Field(default_factory=utc_now)


class MasterAgentState(BaseModel):
    """Cross-turn state snapshot for the master agent.

    Kept on AppState and updated at the start and end of every dispatcher turn
    so operators and the UI can always observe what the agent is currently doing
    without inspecting the raw LangGraph state.

    ``current_phase`` is the only mutable field that changes during a turn.
    Everything else is the immutable identity / provenance of the last completed
    turn and is replaced atomically when a turn finishes.
    """

    # Current lifecycle phase — updated in real time.
    current_phase: TurnPhase = TurnPhase.IDLE

    # Identity of the last (or in-flight) turn.
    conversation_id: str = ""
    task_id: str = ""
    task_type: str = ""

    # Workers that were planned / have run in the last turn.
    planned_workers: tuple[str, ...] = ()
    completed_workers: tuple[str, ...] = ()

    # Memory recall summary for the last turn.
    recall_count: int = 0
    # How many new items were proposed to memory from the last turn's results.
    memory_proposed_count: int = 0

    # Timestamps for latency tracking.
    turn_started_at: datetime | None = None
    turn_finished_at: datetime | None = None

    def start_turn(
        self,
        conversation_id: str,
        task_id: str,
        task_type: str,
    ) -> None:
        """Transition to ROUTING at the start of a new turn."""
        self.current_phase = TurnPhase.ROUTING
        self.conversation_id = conversation_id
        self.task_id = task_id
        self.task_type = task_type
        self.planned_workers = ()
        self.completed_workers = ()
        self.recall_count = 0
        self.memory_proposed_count = 0
        self.turn_started_at = utc_now()
        self.turn_finished_at = None

    def finish_turn(self, phase: TurnPhase = TurnPhase.DONE) -> None:
        """Transition to DONE (or ERROR) at the end of a turn."""
        self.current_phase = phase
        self.turn_finished_at = utc_now()
