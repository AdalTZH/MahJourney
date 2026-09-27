from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

from fastapi import (
    APIRouter,
    Cookie,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from .agents import (
    AgentContext,
    classify_task_type,
    is_cancellation,
    is_confirmation,
    is_plan_approval_request,
)
from .auth import (
    SESSION_COOKIE_NAME,
    issue_session_token,
    verify_password,
    verify_session_token,
)
from .dispatch import format_driver_reply, send_route_messages
from .disruptions import (
    DEFAULT_ROAD_BUFFER_M,
    PLANNING_DISRUPTION_TYPES,
    _polygon_from_payload,
    active_disruptions,
    buffer_path_to_polygon,
    disruption_speed_kph_by_stop,
    path_intersects_polygon,
    point_in_polygon,
)
from .domain import (
    AgentTask,
    ConversationMessage,
    Coordinate,
    DisruptionEvent,
    DisruptionType,
    Order,
    PlanVersion,
    PolicyInput,
    SnapshotStatus,
    TurnPhase,
    Vehicle,
    VehicleRoute,
)
from .evaluation import run_evaluation as evaluate_scenarios
from .integrations import summarize_traffic_conditions, summarize_weather_conditions
from .planning import (
    _build_route,
    _ortools_order,
    assign_orders_to_depots,
    build_plan,
    depot_node_id,
    greedy_baseline,
    plan_delta,
    reapply_route_timing,
    validate_plan,
)
from .policy import decide_policy
from .route_geometry import (
    decode_polyline,
    enrich_plan_geometry,
    enrich_plan_geometry_avoiding,
    enrich_plan_geometry_graphhopper,
    road_distance_matrix,
)
from .simulation import vehicle_progress

logger = logging.getLogger(__name__)

# Exception classes treated as a transient persistence-layer outage on the
# dispatch path (DB connection lost, pool exhausted, network blip to Postgres
# or the OpenAI embedding endpoint). Caught narrowly around individual
# persistence calls so a turn degrades gracefully (less context, no save)
# instead of failing the whole request. Programming errors (TypeError,
# KeyError, etc.) are intentionally NOT swallowed here.
_PERSISTENCE_OUTAGE_ERRORS = (SQLAlchemyError, ConnectionError, OSError, TimeoutError)

# Above this many CURATED POLICY items, every dispatcher turn is unconditionally
# injecting a lot of prompt tokens (see _prepare_dispatch's always-injected
# by_kind("POLICY") recall). POLICY items are never capped or dropped — a
# silently-missing policy is worse than a bigger prompt — but this threshold
# gives early visibility into the token-cost/noise tradeoff before it becomes a
# real problem, rather than discovering it from a latency or cost complaint.
_POLICY_COUNT_WARN_THRESHOLD = 50

# R10 leg-avoidance uses a wider corridor than the 40 m zone used for the
# affects/speed check. The solver tests straight stop-to-stop segments: a
# narrow corridor can be bypassed by a diagonal leg between distant stops
# even if the real driven road crosses the closure. 500 m gives the avoidance
# polygon enough width to catch those diagonals reliably on a city-scale map.
REROUTE_AVOIDANCE_BUFFER_M = 500.0

router = APIRouter(prefix="/api/v1")
# Routes that must stay reachable without a session: liveness checks, the
# Telegram webhook (already authenticated by its own secret-token header),
# and the login flow itself. Everything on `router` above requires a valid
# admin session cookie once mounted with `require_admin` in main.py.
public_router = APIRouter(prefix="/api/v1")


def app_state(request: Request):
    return request.app.state.services


class LoginRequest(BaseModel):
    username: str
    password: str


def require_admin(
    request: Request, mahjourney_session: str | None = Cookie(default=None)
) -> str:
    """FastAPI dependency gating every route mounted with it behind a login.

    Raises 401 when there is no session cookie or it fails to verify (wrong
    signature, tampered, or expired) so a caller can distinguish "log in" from
    a genuine permission error.
    """
    settings = app_state(request).settings
    username = verify_session_token(mahjourney_session or "", settings.app_session_secret)
    if username is None:
        raise HTTPException(401, "authentication required")
    return username


@public_router.post("/auth/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, str]:
    state = app_state(request)
    settings = state.settings
    client_key = request.client.host if request.client else "unknown"
    if state.login_throttle.is_locked(client_key):
        raise HTTPException(429, "too many failed login attempts, try again shortly")
    valid = hmac.compare_digest(
        body.username, settings.admin_username
    ) and verify_password(body.password, settings.admin_password_hash)
    if not valid:
        state.login_throttle.record_failure(client_key)
        await state.record_audit("ADMIN_LOGIN_FAILED", client_key, {"username": body.username})
        raise HTTPException(401, "invalid username or password")
    state.login_throttle.clear(client_key)
    await state.record_audit("ADMIN_LOGIN_SUCCEEDED", settings.admin_username, {})
    token = issue_session_token(
        settings.admin_username, settings.app_session_secret, settings.session_ttl_minutes
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "production",
    )
    return {"username": settings.admin_username}


@public_router.post("/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"logged_out": True}


@public_router.get("/auth/session")
def session_status(
    request: Request, mahjourney_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    settings = app_state(request).settings
    username = verify_session_token(mahjourney_session or "", settings.app_session_secret)
    return {"authenticated": username is not None, "username": username}


class GeneratePlanRequest(BaseModel):
    source_data_version: str = "fixture-v1"
    parent_plan_id: str | None = None


class ActivatePlanRequest(BaseModel):
    plan_id: str
    version: int
    # When True (default), activation also dispatches route messages to drivers
    # over Telegram. The dispatcher UI can activate a draft without sending by
    # passing dispatch=False, then send explicitly from the Plan Detail tab.
    dispatch: bool = True


class ClockUpdate(BaseModel):
    playing: bool | None = None
    speed: int | None = None
    current_minute: int | None = None


class BranchRequest(BaseModel):
    at_minute: int = Field(ge=0, le=1439)


class PolygonDisruptionRequest(BaseModel):
    """Body for both the disruption preview and apply endpoints.

    A zone is described in one of two ways:

    * ``polygon`` — a user-drawn zone (at least 3 points, forming a ring — see
      :func:`mahjourney.disruptions.point_in_polygon`), or
    * ``road_path`` + ``buffer_m`` — a selected road polyline (at least 2
      points) that the backend buffers into a thin corridor polygon via
      :func:`mahjourney.disruptions.buffer_path_to_polygon`. This form is only
      valid for ``ROAD_CLOSURE``.

    Exactly one of the two must be supplied. ``severity`` is only meaningful for
    ``HEAVY_RAIN`` and defaults to the module's default when omitted.
    """

    disruption_type: DisruptionType
    polygon: list[Coordinate] | None = None
    road_path: list[Coordinate] | None = None
    buffer_m: float | None = Field(default=None, gt=0)
    severity: str | None = None
    effective_minute: int = Field(ge=0, le=1439)

    @model_validator(mode="after")
    def _validate_shape(self) -> PolygonDisruptionRequest:
        has_polygon = self.polygon is not None and len(self.polygon) >= 3
        has_road = self.road_path is not None and len(self.road_path) >= 2
        if has_polygon == has_road:
            raise ValueError(
                "provide exactly one of polygon (>=3 points) or road_path (>=2 points)"
            )
        if has_road and self.disruption_type != DisruptionType.ROAD_CLOSURE:
            raise ValueError("road_path is only supported for ROAD_CLOSURE")
        return self


class ApprovalCreate(BaseModel):
    plan_id: str
    plan_version: int
    action: dict[str, Any]


class ProofSubmission(BaseModel):
    proof: dict[str, Any]


class EnrollmentIssue(BaseModel):
    driver_id: str


class MemoryProposal(BaseModel):
    kind: str
    content: str
    trust_label: str = "UNTRUSTED_EXTERNAL"


class MemoryReplacement(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class DispatcherMessage(BaseModel):
    conversation_id: str = "dispatcher-demo"
    message: str = Field(min_length=1, max_length=4000)


@public_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mahjourney-api"}


@public_router.get("/ready")
def ready(request: Request) -> dict[str, Any]:
    state = app_state(request)
    return {
        "status": "ready",
        "mode": "fixture-safe" if state.settings.missing_live_credentials() else "live-capable",
        "missing_live_credentials": state.settings.missing_live_credentials(),
        "persistence": "POSTGRESQL" if state.persistence else "IN_MEMORY",
        "data_source": "database",
        "telegram_bot_username": state.settings.telegram_bot_username,
    }


@router.get("/fleet")
def fleet(request: Request) -> dict[str, Any]:
    state = app_state(request)
    depot = state.depots[0].location if state.depots else state.fleet[0].start
    return {
        "depot": depot,
        "depots": state.depots,
        "vehicles": state.fleet,
        "orders": state.orders,
    }


@router.get("/orders")
async def list_orders(request: Request) -> list[dict[str, Any]]:
    state = app_state(request)
    if state.repository:
        return list(await state.repository.list_orders_raw())
    return [order.model_dump() for order in state.orders]


@router.get("/vehicles")
async def list_vehicles(request: Request) -> list[dict[str, Any]]:
    state = app_state(request)
    if state.repository:
        return list(await state.repository.list_vehicles_raw())
    return [vehicle.model_dump() for vehicle in state.fleet]


@router.get("/drivers")
async def list_drivers(request: Request) -> list[dict[str, Any]]:
    state = app_state(request)
    if state.repository:
        return list(await state.repository.list_drivers_raw())
    # No repository configured; surface the driver ids currently in the fleet.
    return [{"driver_id": vehicle.driver_id} for vehicle in state.fleet]


@router.get("/depots")
async def list_depots(request: Request) -> list[dict[str, Any]]:
    state = app_state(request)
    if state.repository:
        return list(await state.repository.list_depots_raw())
    return [depot.model_dump() for depot in state.depots]


async def _precompute_road_matrix(state) -> dict[tuple[str, str], float]:
    """Build a OneMap road-distance matrix scoped per depot cluster.

    For each depot, the node set is that depot's vehicles' start points plus the
    orders assigned to the depot. This covers every intra-depot pair the planner
    could query while keeping the OneMap call count bounded (no cross-depot
    pairs). Results merge into one flat ``{(from_id, to_id): km}`` dict keyed by
    ``depot_node_id(start)`` and order ids, matching how the planner looks legs up.
    """
    assigned, _ = assign_orders_to_depots(state.orders, state.depots)
    orders_by_depot: dict[str, list] = {}
    for order in assigned:
        orders_by_depot.setdefault(order.assigned_depot_id, []).append(order)
    vehicles_by_depot: dict[str, list] = {}
    for vehicle in state.fleet:
        vehicles_by_depot.setdefault(vehicle.depot_id, []).append(vehicle)

    matrix: dict[tuple[str, str], float] = {}
    for depot_id, depot_orders in orders_by_depot.items():
        depot_vehicles = vehicles_by_depot.get(depot_id, [])
        if not depot_vehicles or not depot_orders:
            continue
        points: dict[str, Coordinate] = {}
        # Each vehicle's start becomes a depot node keyed by its coordinate.
        for vehicle in depot_vehicles:
            points[depot_node_id(vehicle.start)] = vehicle.start
        for order in depot_orders:
            points[order.order_id] = order.location
        cluster = await road_distance_matrix(tuple(points.items()), state.onemap)
        matrix.update(cluster)
    return matrix


async def _compute_candidate_plan(
    state,
    *,
    source_data_version: str,
    parent_plan_id: str | None,
    scenario_id: str = "demo",
    extra_disruptions: tuple[DisruptionEvent, ...] = (),
):
    """Compute a candidate plan WITHOUT persisting it.

    This is the pure-computation core shared by :func:`_generate_and_store_plan`
    (which persists and audits) and the Scenario-Laboratory injection path
    (which must NOT touch live plan history, so the map/latest_plan stay put).
    It runs the same real pipeline — LTA speed context, active-disruption speed
    penalties, optional OneMap road matrix, ``build_plan``, and OneMap geometry
    enrichment — and returns the resulting CANDIDATE/VALIDATED plan. It never
    appends to ``state.plans`` or calls ``save_plan``/``record_audit``.

    ``extra_disruptions`` folds in additional disruption events that are NOT
    (yet) part of ``state.simulation``'s event list — used by the disruption
    preview endpoint to show "what would happen" from a drawn zone without
    injecting it.
    """
    if parent_plan_id and parent_plan_id in state.plans:
        plan_id = parent_plan_id
        version = state.plans[plan_id][-1].version + 1
    else:
        plan_id = None
        version = 1
    speed_context: dict[str, float] | None = None
    if state.repository:
        speed_context, speed_version = await state.repository.nearest_speed_context(
            tuple(
                (order.order_id, order.location.lat, order.location.lon)
                for order in state.orders
            )
        )
        if speed_version:
            source_data_version = f"{source_data_version}+lta-v4:{speed_version}"
    # Active road-closure/heavy-rain disruptions compound on top of whatever
    # traffic context was already found, so the plan reflects both.
    scenario_events = state.simulation.events(scenario_id) + extra_disruptions
    current_minute = state.simulation.get(scenario_id).current_minute
    active = active_disruptions(scenario_events, current_minute)
    if active:
        speed_context = disruption_speed_kph_by_stop(state.orders, active, speed_context)
        active_types = ",".join(sorted({str(e.event_type) for e in active}))
        source_data_version = f"{source_data_version}+disruption:{active_types}"
    # Optionally optimize each vehicle's visit order on real OneMap road
    # distance. The matrix is precomputed per depot cluster here (async) and
    # handed to the synchronous planner; any pair that fails to route is simply
    # absent, so the planner falls back to straight-line distance for it.
    road_matrix: dict[tuple[str, str], float] | None = None
    settings = state.settings
    if settings.road_optimized_routing and settings.onemap_access_token and state.depots:
        road_matrix = await _precompute_road_matrix(state)
        if road_matrix:
            source_data_version = f"{source_data_version}+road:onemap"
    plan = build_plan(
        state.fleet,
        state.orders,
        enforce_delivery_windows=state.settings.enforce_delivery_windows,
        plan_id=plan_id,
        version=version,
        source_data_version=source_data_version,
        speed_kph_by_stop=speed_context,
        depots=state.depots,
        max_stops_per_vehicle=state.settings.max_stops_per_vehicle,
        road_distance_km=road_matrix,
        ortools_time_limit_seconds=state.settings.ortools_time_limit_seconds,
    )
    if state.settings.onemap_access_token:
        depot_by_vehicle = {vehicle.vehicle_id: vehicle.start for vehicle in state.fleet}
        plan = await enrich_plan_geometry(
            plan, state.fleet[0].start, state.onemap, depot_by_vehicle
        )
    return plan


def filter_candidate_routes(candidate: PlanVersion, vehicle_ids: tuple[str, ...]) -> list:
    """The candidate plan's routes restricted to the given vehicle ids, in the
    candidate's original order. Used by the disruption preview/apply flow so
    the response only surfaces the vehicles actually affected by the drawn
    zone, not the whole fleet-wide replan."""
    wanted = set(vehicle_ids)
    return [route for route in candidate.routes if route.vehicle_id in wanted]


async def _generate_and_store_plan(
    state,
    *,
    source_data_version: str,
    parent_plan_id: str | None,
    actor: str,
    audit_event: str,
    audit_payload_extra: dict[str, Any] | None = None,
    scenario_id: str = "demo",
):
    """Compute a new candidate plan and append it to the plan history.

    This only ever produces a CANDIDATE/VALIDATED plan; it never activates
    anything. Whether the caller is the operator (``/plans/generate``) or an
    agent proposal, the resulting plan sits alongside the current ACTIVE plan
    until a human explicitly activates it via ``/plans/activate``.
    """
    plan = await _compute_candidate_plan(
        state,
        source_data_version=source_data_version,
        parent_plan_id=parent_plan_id,
        scenario_id=scenario_id,
    )
    state.plans[plan.plan_id].append(plan)
    await state.save_plan(plan)
    payload = {"plan_id": plan.plan_id, "version": plan.version, **(audit_payload_extra or {})}
    await state.record_audit(audit_event, actor, payload)
    return plan


@router.post("/plans/generate")
async def generate_plan(body: GeneratePlanRequest, request: Request):
    state = app_state(request)
    # Wall-clock time to compute + persist the plan, so the operator sees how
    # long a generation actually took (it scales with the OR-Tools search
    # budget). Returned as generation_ms alongside the plan fields rather than
    # on the frozen PlanVersion model, so no other plan-producing path changes.
    started = perf_counter()
    plan = await _generate_and_store_plan(
        state,
        source_data_version=body.source_data_version,
        parent_plan_id=body.parent_plan_id,
        actor="dispatcher",
        audit_event="PLAN_GENERATED",
    )
    generation_ms = round((perf_counter() - started) * 1000)
    return {**plan.model_dump(mode="json"), "generation_ms": generation_ms}


@router.get("/plans")
def list_plans(request: Request):
    state = app_state(request)
    return [version for versions in state.plans.values() for version in versions]


@router.get("/plans/drafts")
def list_draft_plans(request: Request) -> list[dict[str, Any]]:
    """Pending candidate plans awaiting the dispatcher's decision.

    The live map and Plan Detail show only the ACTIVE plan, so a freshly
    generated candidate the dispatcher has not activated yet lives here instead.
    Returns the latest version of every plan that is neither ACTIVE nor already
    rejected (SUPERSEDED), newest first, so the dispatcher can review each draft
    and either activate or reject it. Usually one, but the agent path can
    propose more than one.
    """
    state = app_state(request)
    drafts = [
        versions[-1]
        for versions in state.plans.values()
        if versions and versions[-1].status in {"CANDIDATE", "VALIDATED"}
    ]
    drafts.sort(key=lambda plan: plan.created_at, reverse=True)
    return [plan.model_dump(mode="json") for plan in drafts]


@router.get("/plans/{plan_id}/versions/{version}")
def get_plan(plan_id: str, version: int, request: Request):
    state = app_state(request)
    for plan in state.plans.get(plan_id, []):
        if plan.version == version:
            return plan
    raise HTTPException(404, "plan version not found")


@router.get("/plans/{plan_id}/delta")
def get_plan_delta(plan_id: str, request: Request, from_version: int, to_version: int):
    state = app_state(request)
    by_version = {plan.version: plan for plan in state.plans.get(plan_id, [])}
    if from_version not in by_version or to_version not in by_version:
        raise HTTPException(404, "plan version not found")
    return plan_delta(by_version[from_version], by_version[to_version])


@router.post("/plans/{plan_id}/versions/{version}/validate")
def validate(plan_id: str, version: int, request: Request):
    state = app_state(request)
    plan = get_plan(plan_id, version, request)
    violations = validate_plan(
        plan, state.fleet, state.orders, state.settings.max_stops_per_vehicle
    )
    return {"valid": not violations, "hard_violations": violations}


@router.get("/plans/compare/baselines")
def compare_baselines(request: Request):
    state = app_state(request)
    candidate = state.latest_plan
    if candidate is None:
        raise HTTPException(409, "no plan has been generated yet")
    baseline = greedy_baseline(state.fleet, state.orders)
    improvement = (
        (baseline.objective_cost - candidate.objective_cost) / baseline.objective_cost * 100
    )
    return {
        "greedy_cost": baseline.objective_cost,
        "mahjourney_cost": candidate.objective_cost,
        "improvement_percent": round(improvement, 1),
    }


async def _dispatch_plan_to_drivers(state, plan: PlanVersion) -> dict[str, bool]:
    """Best-effort: send each route's driver their stops over Telegram.

    A driver only receives a message if their Telegram account is enrolled
    and bound (see /telegram/webhook). Missing bindings and send failures are
    both reported as False rather than raised, so one driver being
    unreachable never blocks the others or the plan activation itself.
    """
    sent: dict[str, bool] = {}
    for route in plan.routes:
        telegram_user_id = state.enrollment.telegram_user_for(route.driver_id)
        if telegram_user_id is None:
            sent[route.driver_id] = False
            continue
        sent[route.driver_id] = await send_route_messages(
            state.telegram, telegram_user_id, route, state.orders, plan, state.fleet,
            depot=next((v.start for v in state.fleet if v.vehicle_id == route.vehicle_id), None),
        )
    return sent


async def _dispatch_route_to_driver(
    state, driver_id: str
) -> tuple[bool, str]:
    """Send ONE driver their route from the current live plan over Telegram.

    A single-driver counterpart to :func:`_dispatch_plan_to_drivers`, used by the
    agent's confirm-then-send flow so the dispatcher can hand one driver their
    schedule without dispatching the whole fleet. Returns ``(sent, reason)``:
    ``sent`` is whether the Telegram message went out, and ``reason`` is a short
    machine/human tag explaining a failure (``"no_route"``, ``"not_enrolled"``,
    ``"send_failed"``) or ``"sent"`` on success — mirroring the best-effort,
    never-raise contract of the fleet-wide dispatch.
    """
    plan = state.latest_plan
    if plan is None:
        return False, "no_route"
    route = next((r for r in plan.routes if r.driver_id == driver_id), None)
    if route is None:
        return False, "no_route"
    telegram_user_id = state.enrollment.telegram_user_for(driver_id)
    if telegram_user_id is None:
        return False, "not_enrolled"
    sent = await send_route_messages(
        state.telegram, telegram_user_id, route, state.orders, plan, state.fleet,
        depot=next((v.start for v in state.fleet if v.vehicle_id == route.vehicle_id), None),
    )
    if not sent:
        return False, "send_failed"
    await state.record_audit(
        "DRIVER_ROUTE_DISPATCHED",
        "dispatcher",
        {
            "driver_id": driver_id,
            "vehicle_id": route.vehicle_id,
            "plan_id": plan.plan_id,
            "version": plan.version,
            "stops": len(route.stops),
        },
    )
    return True, "sent"


async def _activate_plan_version(
    state,
    plan: PlanVersion,
    *,
    dispatch: bool,
    actor: str = "dispatcher",
) -> PlanVersion:
    """Flip a candidate/validated plan to ACTIVE and (optionally) dispatch it.

    The single source of truth for what "activate" means — shared by the HTTP
    endpoint and the agent's confirm-then-activate flow so the two can never
    diverge. The caller is responsible for the hard-violations guard (the HTTP
    endpoint raises 409; the agent path declines with a spoken reply before
    parking), so this helper assumes the plan is activatable.
    """
    state.active_plan_id = plan.plan_id
    activated = plan.model_copy(update={"status": "ACTIVE"})
    versions = state.plans[plan.plan_id]
    versions[versions.index(plan)] = activated
    await state.save_plan(activated)
    await state.record_audit(
        "PLAN_ACTIVATED", actor, {"plan_id": plan.plan_id, "version": plan.version}
    )
    if dispatch:
        dispatched = await _dispatch_plan_to_drivers(state, activated)
        await state.record_audit(
            "PLAN_DISPATCHED_TO_DRIVERS",
            actor,
            {"plan_id": plan.plan_id, "version": plan.version, "sent": dispatched},
        )
    return activated


@router.post("/plans/activate")
async def activate_plan(body: ActivatePlanRequest, request: Request):
    state = app_state(request)
    if state.settings.app_env == "test" and not state.settings.allow_plan_execution_in_tests:
        raise HTTPException(403, "plan execution is disabled in tests")
    plan = get_plan(body.plan_id, body.version, request)
    if plan.hard_violations:
        raise HTTPException(409, "a plan with hard violations cannot be activated")
    return await _activate_plan_version(state, plan, dispatch=body.dispatch)


@router.post("/plans/reject")
async def reject_plan(body: ActivatePlanRequest, request: Request):
    """Reject a draft candidate the dispatcher does not want to activate.

    Marks the plan version SUPERSEDED so it drops out of the drafts list
    (``/plans/drafts``) without ever going live. The active plan is untouched —
    rejecting a draft never changes what the fleet is running. Rejecting the
    plan that happens to be active is refused, since that would leave the fleet
    with no live plan.
    """
    state = app_state(request)
    plan = get_plan(body.plan_id, body.version, request)
    # Guard: only block rejection if this exact version IS the active plan.
    # A reroute candidate shares the active plan's plan_id (it's the next
    # version of the same plan group) but has not been activated — blocking
    # by plan_id alone would prevent rejecting it, which is incorrect.
    active_version = None
    if state.active_plan_id == plan.plan_id:
        active_versions = state.plans.get(plan.plan_id, [])
        active_version = next(
            (p.version for p in active_versions if p.status == "ACTIVE"), None
        )
    if plan.status == "ACTIVE" or active_version == plan.version:
        raise HTTPException(409, "cannot reject the active plan")
    rejected = plan.model_copy(update={"status": "SUPERSEDED"})
    versions = state.plans[plan.plan_id]
    versions[versions.index(plan)] = rejected
    await state.save_plan(rejected)
    await state.record_audit(
        "PLAN_REJECTED", "dispatcher", {"plan_id": plan.plan_id, "version": plan.version}
    )
    return rejected


@router.post("/plans/{plan_id}/versions/{version}/dispatch")
async def redispatch_plan(plan_id: str, version: int, request: Request):
    """Resend the current route messages for an already-activated plan.

    Useful when a driver missed the original Telegram message (e.g. they
    enrolled after activation, or a send failed) without needing to
    reactivate the plan.
    """
    state = app_state(request)
    plan = get_plan(plan_id, version, request)
    if plan.status != "ACTIVE":
        raise HTTPException(409, "only an active plan can be dispatched to drivers")
    dispatched = await _dispatch_plan_to_drivers(state, plan)
    await state.record_audit(
        "PLAN_DISPATCHED_TO_DRIVERS",
        "dispatcher",
        {"plan_id": plan.plan_id, "version": plan.version, "sent": dispatched},
    )
    return {"plan_id": plan.plan_id, "version": plan.version, "sent": dispatched}


@router.post("/policy/decide")
def policy_decision(body: PolicyInput):
    return decide_policy(body)


class RoadPathRequest(BaseModel):
    """Two endpoints of a road to route between for road-closure selection."""

    start: Coordinate
    end: Coordinate


@router.post("/scenario/{scenario_id}/road-path")
async def scenario_road_path(scenario_id: str, body: RoadPathRequest, request: Request):
    """Route a start->end pair to a real road polyline for road-closure selection.

    Calls OneMap's driving route and decodes its polyline into ``road_path``
    (a list of ``{lat, lon}`` points following the actual road), plus the route
    distance in km. This is the geometry the frontend draws for the selected
    road and hands back as the ``road_path`` of a ROAD_CLOSURE disruption.

    Raises 503 when OneMap is not configured or the route lookup fails, so the
    caller can fall back to a straight segment between the two clicks.
    """
    state = app_state(request)
    if not state.settings.onemap_access_token:
        raise HTTPException(503, "OneMap is not configured")
    try:
        response = await state.onemap.route(
            (body.start.lat, body.start.lon), (body.end.lat, body.end.lon)
        )
    except Exception as exc:  # noqa: BLE001 - any OneMap/transport failure -> fall back
        raise HTTPException(503, f"road routing unavailable: {exc}") from exc
    encoded = response.get("route_geometry")
    if not isinstance(encoded, str) or not encoded:
        raise HTTPException(503, "road routing returned no geometry")
    try:
        road_path = decode_polyline(encoded)
    except (IndexError, ValueError) as exc:
        raise HTTPException(503, "road routing geometry could not be decoded") from exc
    if len(road_path) < 2:
        raise HTTPException(503, "road routing returned a degenerate path")
    distance_km = float(response.get("route_summary", {}).get("total_distance", 0)) / 1000
    return {"road_path": list(road_path), "distance_km": round(distance_km, 3)}


@router.get("/scenario/{scenario_id}")
def scenario(scenario_id: str, request: Request):
    state = app_state(request)
    try:
        return {
            "clock": state.simulation.get(scenario_id),
            "events": state.simulation.events(scenario_id),
        }
    except KeyError as exc:
        raise HTTPException(404, "scenario not found") from exc


@router.patch("/scenario/{scenario_id}")
async def update_scenario(scenario_id: str, body: ClockUpdate, request: Request):
    state = app_state(request)
    try:
        clock = state.simulation.update(scenario_id, **body.model_dump())
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # The clock moving forward can cross a disruption's effective time even if
    # nothing new was injected, so the live plan's timing is re-checked here too.
    await _apply_disruption_to_live_plan(state, scenario_id)
    return clock


@router.post("/scenario/{scenario_id}/reset")
def reset_scenario(scenario_id: str, request: Request):
    return app_state(request).simulation.reset(scenario_id)


@router.post("/scenario/{scenario_id}/branch")
def branch_scenario(scenario_id: str, body: BranchRequest, request: Request):
    try:
        return app_state(request).simulation.branch(scenario_id, body.at_minute)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/disruptions")
async def inject_disruption(body: DisruptionEvent, request: Request):
    state = app_state(request)
    try:
        event = state.simulation.inject(body)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if state.persistence:
        await state.persistence.save_disruption(event)
    await state.record_audit("DISRUPTION_INJECTED", "dispatcher", body.model_dump(mode="json"))
    # A ROAD_CLOSURE proposes a mid-route reroute candidate; anything else (or a
    # closure nothing can reroute) re-times the live plan in place.
    await _handle_disruption_effect(state, event.scenario_id, event)
    return event


async def _apply_disruption_to_live_plan(state, scenario_id: str) -> None:
    """Recompute the current live plan's timing if a disruption is now active.

    This keeps the existing stop assignment and sequence (it is not a replan)
    but updates travel times, ETAs, and durations so the plan the dispatcher and
    map are already showing reflects the delay, not just plans generated after
    the fact.
    """
    current_minute = state.simulation.get(scenario_id).current_minute
    active = active_disruptions(state.simulation.events(scenario_id), current_minute)
    if not active:
        return
    plan = state.latest_plan
    if plan is None:
        return
    # Keyed on the active events' ids (not just their types), so two distinct
    # zones of the same disruption type (e.g. two separate road closures) each
    # trigger a re-time instead of being treated as "already applied" once any
    # ROAD_CLOSURE has been seen.
    active_tag = ",".join(sorted(e.event_id for e in active))
    if plan.source_data_version.startswith("disruption-timing:"):
        applied_tag = plan.source_data_version.split(":", 1)[1].split("+", 1)[0]
        if applied_tag == active_tag:
            # Already reflects exactly this set of active events; nothing new.
            return
        base_source = plan.source_data_version.split("+", 1)[-1]
    else:
        base_source = plan.source_data_version
    speed_context = disruption_speed_kph_by_stop(state.orders, active, None)
    updated = reapply_route_timing(
        plan,
        state.fleet,
        state.orders,
        speed_context,
        version=plan.version + 1,
        source_data_version=f"disruption-timing:{active_tag}+{base_source}",
        enforce_delivery_windows=state.settings.enforce_delivery_windows,
    )
    state.plans[updated.plan_id].append(updated)
    await state.save_plan(updated)
    await state.record_audit(
        "LIVE_PLAN_TIMING_UPDATED",
        "disruption-analysis-agent",
        {
            "plan_id": updated.plan_id,
            "version": updated.version,
            "active_disruptions": [e.event_id for e in active],
            "objective_cost_delta_km": round(updated.objective_cost - plan.objective_cost, 2),
        },
    )


def _require_real_plan(state) -> PlanVersion:
    """Guard: disruption preview/apply only runs against a real, routed plan.

    The frontend gates the draw/inject controls on ``plan_id !== "fixture-plan"``;
    this is the backend-side counterpart. Operational data is DB-only now, so
    ``state.latest_plan`` should always have real routes once startup has
    succeeded — this guard mainly protects against calling the endpoint before
    any plan has been built.
    """
    plan = state.latest_plan
    if plan is None:
        raise HTTPException(409, "no real routed plan is available to disrupt")
    if not any(route.stops for route in plan.routes):
        raise HTTPException(409, "no real routed plan is available to disrupt")
    return plan


def _affected_vehicle_and_order_ids(
    plan: PlanVersion, orders: tuple, polygon: tuple[Coordinate, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Vehicle and order ids affected by a drawn ``polygon`` zone.

    An order is "affected" if its location falls inside the zone.

    A vehicle is "affected" if EITHER it serves an affected order OR its route
    path (``VehicleRoute.geometry``) enters/crosses the zone. The path check is
    what makes a vehicle count when it merely drives through the zone (e.g. a
    truck passing through a heavy-rain area on the way to deliveries outside
    it) — the previous stop-only check missed those, reporting "0 vehicles"
    even when a route line clearly overlapped the zone.

    When a route has no geometry (geometry enrichment disabled/unavailable), it
    falls back to a path built from the route's stop locations so a route still
    counts if the straight line through its stops passes through the zone.

    Both lists are returned in the plan/order's original relative order,
    deduplicated.
    """
    affected_order_ids = tuple(
        order.order_id for order in orders if point_in_polygon(order.location, polygon)
    )
    affected_order_id_set = set(affected_order_ids)
    affected_vehicle_ids = tuple(
        route.vehicle_id
        for route in plan.routes
        if any(stop.stop_id in affected_order_id_set for stop in route.stops)
        or path_intersects_polygon(_route_path(route), polygon)
    )
    return affected_vehicle_ids, affected_order_ids


def _route_path(route) -> tuple[Coordinate, ...]:
    """The polyline to test against a disruption zone for a route.

    Prefers the enriched road geometry; falls back to the ordered stop
    locations when geometry is absent so the path check still has something
    meaningful to test.
    """
    if route.geometry:
        return tuple(route.geometry)
    return tuple(stop.location for stop in route.stops)


def _reroute_one_vehicle(
    route: VehicleRoute,
    vehicle: Vehicle,
    orders: tuple[Order, ...],
    closure_polygon: tuple[Coordinate, ...],
    current_minute: int,
    *,
    speed_kph_by_stop: dict[str, float] | None = None,
    enforce_delivery_windows: bool = True,
) -> VehicleRoute | None:
    """Reroute one affected vehicle's remaining stops around a closure.

    Pure function (no app state) so it is unit-testable: ``orders`` is the full
    set of orders the plan draws from, used to map stop ids back to ``Order``
    objects for the solver.

    Returns the rerouted :class:`VehicleRoute` for this vehicle, or ``None`` to
    fall back to re-time-in-place, in these cases:
    - Case (a): the leg the vehicle is currently driving (``current_leg``) itself
      crosses the closure — a mid-leg diversion is meaningless, so let the
      re-time path handle the delay (R5).
    - No remaining stops to re-sequence (all completed, or only the committed
      stop plus depot return remain) (R4.6).
    - R9 solve-failure: the solver returns fewer stops than the remaining input
      (can't fit them all) — never emit a partial/broken candidate.

    Otherwise: the completed stops and the committed next stop are pinned; only
    the remaining stops (N+2 onward) are re-sequenced, with the closure applied
    both as a stop-level speed penalty (``speed_kph_by_stop``) and a leg-level
    avoidance (``closure_polygon``) so the reroute visibly sequences around the
    closed road. The full ordered route is rebuilt from the depot for internally
    consistent sequences, ETAs, distance, and duration (R7).
    """
    progress = vehicle_progress(route, vehicle, current_minute)

    # Case (a): currently driving on the closed road -> no reroute.
    if progress.current_leg is not None and path_intersects_polygon(
        progress.current_leg, closure_polygon
    ):
        logger.info(
            "reroute skipped for vehicle=%s: current leg crosses the closure "
            "(re-time-in-place applies)",
            vehicle.vehicle_id,
        )
        return None

    # Nothing left to re-sequence.
    if not progress.remaining_stop_ids:
        return None

    order_by_id = {order.order_id: order for order in orders}

    # Pinned prefix: completed stops, then the committed next stop (if any). These
    # keep their planned visit order and are never re-sequenced.
    pinned_ids = list(progress.completed_stop_ids)
    if progress.committed_stop_id is not None:
        pinned_ids.append(progress.committed_stop_id)

    committed_stop = next(
        (stop for stop in route.stops if stop.stop_id == progress.committed_stop_id), None
    )

    remaining_orders = [
        order_by_id[stop_id]
        for stop_id in progress.remaining_stop_ids
        if stop_id in order_by_id
    ]
    if len(remaining_orders) != len(progress.remaining_stop_ids):
        # An order id could not be resolved — treat as a fallback rather than
        # silently dropping stops.
        logger.warning(
            "reroute for vehicle=%s could not resolve every remaining order; falling back",
            vehicle.vehicle_id,
        )
        return None

    # Seed the solver at the committed next stop (its location and departure
    # time) so the re-sequenced tail starts from where the truck is committed to
    # being, and avoids the closure at the leg level.
    if committed_stop is not None:
        seed_coordinate = committed_stop.location
        seed_node_id = committed_stop.stop_id
        seed_minute = committed_stop.departure_minute
    else:
        # No committed stop (e.g. at depot pre-departure): seed at the depot.
        seed_coordinate = vehicle.start
        seed_node_id = depot_node_id(vehicle.start)
        seed_minute = vehicle.working_start_minute

    try:
        tail_order = _ortools_order(
            vehicle,
            remaining_orders,
            speed_kph_by_stop=speed_kph_by_stop,
            enforce_delivery_windows=enforce_delivery_windows,
            start_coordinate=seed_coordinate,
            start_node_id=seed_node_id,
            start_minute=seed_minute,
            closure_polygons=(closure_polygon,),
        )
    except (RuntimeError, ValueError) as exc:  # pragma: no cover - defensive
        logger.warning(
            "reroute solve failed for vehicle=%s: %s; falling back", vehicle.vehicle_id, exc
        )
        return None

    # R9: never present a partial candidate. If the solve could not fit every
    # remaining stop, fall back to re-time-in-place.
    if len(tail_order) != len(remaining_orders):
        logger.warning(
            "reroute for vehicle=%s fit %d of %d remaining stops; falling back to re-time",
            vehicle.vehicle_id,
            len(tail_order),
            len(remaining_orders),
        )
        return None

    # Rebuild the whole route from the depot over pinned prefix + re-sequenced
    # tail, so sequences, ETAs, distance, and duration are internally consistent
    # (R7). The pinned prefix keeps its original relative order.
    pinned_orders = [order_by_id[stop_id] for stop_id in pinned_ids if stop_id in order_by_id]
    full_order = pinned_orders + tail_order
    rerouted = _build_route(
        vehicle,
        full_order,
        speed_kph_by_stop=speed_kph_by_stop,
        enforce_delivery_windows=enforce_delivery_windows,
        closure_polygons=(closure_polygon,),
    )
    # Preserve any existing road geometry key; geometry enrichment for the new
    # tail happens in the group orchestration (task 6).
    return rerouted


def _reroute_candidate_routes(
    plan: PlanVersion,
    fleet: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    closure_polygon: tuple[Coordinate, ...],
    current_minute: int,
    *,
    affected_vehicle_ids: tuple[str, ...],
    speed_kph_by_stop: dict[str, float] | None = None,
    enforce_delivery_windows: bool = True,
) -> tuple[tuple[VehicleRoute, ...], tuple[str, ...]]:
    """Reroute every affected vehicle independently around one closure.

    Pure function (no app state / no persistence) so it is unit-testable.
    Returns ``(routes, rerouted_vehicle_ids)`` where ``routes`` mirrors
    ``plan.routes`` in order: each affected vehicle that produced a reroute gets
    its new route; every other route (unaffected, or an affected vehicle that
    fell back per R5/R9) is carried over unchanged. ``rerouted_vehicle_ids`` are
    the vehicles that actually changed.

    Per-vehicle independence (R8.5): one vehicle falling back (case (a), no
    remaining stops, or a solve-failure) never blocks the others — its original
    route simply carries over.
    """
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in fleet}
    affected = set(affected_vehicle_ids)
    new_routes: list[VehicleRoute] = []
    rerouted_ids: list[str] = []
    for route in plan.routes:
        vehicle = vehicle_by_id.get(route.vehicle_id)
        if vehicle is None or route.vehicle_id not in affected:
            new_routes.append(route)
            continue
        rerouted = _reroute_one_vehicle(
            route,
            vehicle,
            orders,
            closure_polygon,
            current_minute,
            speed_kph_by_stop=speed_kph_by_stop,
            enforce_delivery_windows=enforce_delivery_windows,
        )
        if rerouted is None:
            # Case (a) / no remaining stops / solve-failure: carry original over.
            new_routes.append(route)
        else:
            new_routes.append(rerouted)
            rerouted_ids.append(route.vehicle_id)
    return tuple(new_routes), tuple(rerouted_ids)


def _assemble_reroute_plan(
    routes: tuple[VehicleRoute, ...],
    fleet: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    *,
    plan_id: str,
    version: int,
    source_data_version: str,
    enforce_delivery_windows: bool = True,
) -> PlanVersion:
    """Assemble a candidate ``PlanVersion`` from a rerouted routes set.

    Mirrors ``build_plan``'s finalization: build a provisional CANDIDATE, run
    ``validate_plan``, then promote to VALIDATED (or keep CANDIDATE with the
    hard violations recorded). Never activates anything.
    """
    provisional = PlanVersion(
        plan_id=plan_id,
        version=version,
        status="CANDIDATE",
        source_data_version=source_data_version,
        routes=routes,
        objective_cost=round(sum(route.distance_km for route in routes), 2),
    )
    violations = validate_plan(
        provisional, fleet, orders, enforce_delivery_windows=enforce_delivery_windows
    )
    return provisional.model_copy(
        update={
            "status": "VALIDATED" if not violations else "CANDIDATE",
            "hard_violations": violations,
        }
    )


async def _reroute_affected_vehicles(
    state, scenario_id: str, closure_event: DisruptionEvent
) -> tuple[PlanVersion, tuple[str, ...]] | None:
    """Build ONE candidate plan rerouting every vehicle affected by ``closure_event``.

    Resolves the closure zone, finds all affected vehicles, reroutes each
    independently (:func:`_reroute_one_vehicle`), and assembles a single
    candidate ``PlanVersion`` in which rerouted vehicles carry their new route
    and everyone else is unchanged. The candidate is tagged with the closure
    event id so all suggestions from one closure form a retrievable group
    (R8.2), enriched with road geometry for the dotted-line map view, appended
    to plan history as a CANDIDATE (never activated), and audited.

    Returns ``(candidate, rerouted_vehicle_ids)``, or ``None`` when the closure
    resolves to no zone or no vehicle produced a reroute (every affected vehicle
    fell back), in which case the caller sticks with re-time-in-place.
    """
    polygon = _polygon_from_payload(closure_event.payload)
    if polygon is None:
        return None

    plan = state.active_plan
    if plan is None or not any(route.stops for route in plan.routes):
        return None

    affected_vehicle_ids, _ = _affected_vehicle_and_order_ids(plan, state.orders, polygon)
    if not affected_vehicle_ids:
        return None

    # Closure (+ any already-active disruptions) as a stop-level speed penalty,
    # compounded on top of nothing else here (LTA context is folded in at full
    # plan generation; a reroute works from the active plan's assumptions).
    current_minute = state.simulation.get(scenario_id).current_minute
    active = active_disruptions(
        state.simulation.events(scenario_id) + (closure_event,), current_minute
    )
    speed_context = disruption_speed_kph_by_stop(state.orders, active, None)

    # R10 leg-avoidance uses a WIDER corridor than the 40 m zone used for the
    # affects/speed check. The solver tests straight stop-to-stop segments: a
    # 40 m corridor can be missed by a diagonal leg between stops several km
    # apart even if the real road through them crosses the closure. A 500 m
    # half-width makes the avoidance zone large enough that any leg whose
    # natural road path goes through the closure will have its straight
    # approximation caught by the polygon test.
    # For an explicit polygon disruption, use it as-is (the dispatcher drew it).
    road_path_raw = closure_event.payload.get("road_path")
    if road_path_raw is not None:
        try:
            road_path = tuple(
                Coordinate(lat=float(p["lat"]), lon=float(p["lon"])) for p in road_path_raw
            )
            avoidance_polygon = (
                buffer_path_to_polygon(road_path, REROUTE_AVOIDANCE_BUFFER_M) or polygon
            )
        except (KeyError, TypeError, ValueError):
            avoidance_polygon = polygon
    else:
        avoidance_polygon = polygon

    routes, rerouted_ids = _reroute_candidate_routes(
        plan,
        state.fleet,
        state.orders,
        avoidance_polygon,
        current_minute,
        affected_vehicle_ids=affected_vehicle_ids,
        speed_kph_by_stop=speed_context,
        enforce_delivery_windows=state.settings.enforce_delivery_windows,
    )
    if not rerouted_ids:
        # Every affected vehicle fell back (case a / no remaining / solve-fail).
        return None

    # One candidate holding every rerouted vehicle. Tag with the closure event
    # id so the group is retrievable/renderable together (R8.2), and version it
    # off the active plan.
    plan_id = plan.plan_id
    version = state.plans[plan_id][-1].version + 1 if plan_id in state.plans else plan.version + 1
    candidate = _assemble_reroute_plan(
        routes,
        state.fleet,
        state.orders,
        plan_id=plan_id,
        version=version,
        source_data_version=f"mid-route-reroute:{closure_event.event_id}+{plan.source_data_version}",
        enforce_delivery_windows=state.settings.enforce_delivery_windows,
    )
    # Road geometry for the dotted suggestion lines (whole plan re-enriched from
    # stop locations). Best-effort: only when OneMap is configured.
    # Priority order for closure reroutes:
    #   1. GraphHopper (if GRAPHHOPPER_API_KEY is set) — hard-blocks the closure
    #      polygon so the drawn geometry physically avoids the closed road.
    #   2. OneMap with detour waypoint (fallback when GH not configured) — tries
    #      to steer legs via a perpendicular offset point.
    #   3. Plain OneMap — for non-road-path closures (polygon-drawn zones).
    if state.settings.onemap_access_token:
        depot_by_vehicle = {vehicle.vehicle_id: vehicle.start for vehicle in state.fleet}
        road_path_raw = closure_event.payload.get("road_path")
        if road_path_raw is not None:
            try:
                road_path_coords = tuple(
                    Coordinate(lat=float(p["lat"]), lon=float(p["lon"])) for p in road_path_raw
                )
                # Build the avoidance polygon for GH (same 500m corridor used
                # by the solver, wide enough to catch the real road).
                gh_avoid_polygon = (
                    buffer_path_to_polygon(road_path_coords, REROUTE_AVOIDANCE_BUFFER_M)
                    or avoidance_polygon
                )
                if state.graphhopper.configured:
                    candidate = await enrich_plan_geometry_graphhopper(
                        candidate,
                        state.fleet[0].start,
                        state.onemap,
                        state.graphhopper,
                        gh_avoid_polygon,
                        road_path=road_path_coords,
                        depot_by_vehicle=depot_by_vehicle,
                    )
                else:
                    candidate = await enrich_plan_geometry_avoiding(
                        candidate,
                        state.fleet[0].start,
                        state.onemap,
                        road_path_coords,
                        depot_by_vehicle=depot_by_vehicle,
                    )
            except (KeyError, TypeError, ValueError):
                candidate = await enrich_plan_geometry(
                    candidate, state.fleet[0].start, state.onemap, depot_by_vehicle
                )
        else:
            candidate = await enrich_plan_geometry(
                candidate, state.fleet[0].start, state.onemap, depot_by_vehicle
            )

    # SCENARIO-ONLY: the reroute candidate is NOT appended to ``state.plans`` and
    # NOT persisted. Keeping it out of shared state means it never surfaces in
    # ``/plans/drafts`` (dispatcher Draft Plans tab) and can never become the
    # active plan in the Overview — it exists purely to drive the dotted
    # suggestion drawn on the scenario map. Its routes+geometry are returned
    # inline in the apply-disruption response instead of being re-fetched from
    # the drafts list. The audit trail still records that a reroute was
    # suggested, for observability.
    await state.record_audit(
        "MID_ROUTE_REROUTE_SUGGESTED",
        "disruption-analysis-agent",
        {
            "plan_id": candidate.plan_id,
            "version": candidate.version,
            "closure_event_id": closure_event.event_id,
            "rerouted_vehicle_ids": list(rerouted_ids),
        },
    )
    return candidate, rerouted_ids


async def _handle_disruption_effect(
    state, scenario_id: str, event: DisruptionEvent
) -> tuple[PlanVersion, tuple[str, ...]] | None:
    """React to a just-injected disruption event.

    For a ROAD_CLOSURE (v1 scope), attempt a mid-route reroute: this proposes a
    CANDIDATE plan (dotted-line suggestion) that re-sequences the remaining stops
    of every affected vehicle around the closure, for the dispatcher to approve
    via ``/plans/activate``. When the reroute yields a candidate, the live plan's
    timing is intentionally NOT re-timed in place — the candidate is the proposed
    response and the active plan stays put until approved.

    In every other case — a non-closure disruption, or a closure where no vehicle
    could reroute (all affected vehicles are mid-leg on the closed road, have no
    remaining stops, or the solve could not fit them, per R5/R9) — fall back to
    the existing re-time-in-place behavior so the active plan's ETAs still reflect
    the delay. Returns ``(candidate, rerouted_vehicle_ids)`` if a reroute was
    produced, else ``None``.
    """
    result: tuple[PlanVersion, tuple[str, ...]] | None = None
    if event.event_type == DisruptionType.ROAD_CLOSURE:
        result = await _reroute_affected_vehicles(state, scenario_id, event)
    if result is None:
        await _apply_disruption_to_live_plan(state, scenario_id)
    return result


def _validate_disruption_type(disruption_type: DisruptionType) -> None:
    if disruption_type not in PLANNING_DISRUPTION_TYPES:
        raise HTTPException(
            400,
            f"{disruption_type} is not supported for zone-based injection "
            f"(only {', '.join(str(t) for t in PLANNING_DISRUPTION_TYPES)} are)",
        )


def _disruption_payload(body: PolygonDisruptionRequest) -> dict[str, Any]:
    """Build the stored disruption payload from the request's zone shape.

    A polygon zone is stored as ``polygon``; a selected road is stored as
    ``road_path`` + ``buffer_m`` (the buffer defaulting to the module default),
    which :func:`mahjourney.disruptions._polygon_from_payload` later resolves to
    the buffered corridor polygon that drives the closure slowdown.
    """
    payload: dict[str, Any] = {}
    if body.road_path is not None and len(body.road_path) >= 2:
        payload["road_path"] = [{"lat": point.lat, "lon": point.lon} for point in body.road_path]
        payload["buffer_m"] = body.buffer_m if body.buffer_m is not None else DEFAULT_ROAD_BUFFER_M
    else:
        payload["polygon"] = [
            {"lat": point.lat, "lon": point.lon} for point in (body.polygon or [])
        ]
    if body.disruption_type == DisruptionType.HEAVY_RAIN:
        payload["severity"] = (body.severity or "HEAVY").upper()
    return payload


def _resolve_zone_polygon(body: PolygonDisruptionRequest) -> tuple[Coordinate, ...]:
    """The effective containment polygon for a disruption request.

    Uses the drawn ``polygon`` directly, or buffers the selected ``road_path``
    into a corridor polygon (same shape the stored payload later resolves to),
    so :func:`_affected_vehicle_and_order_ids` matches on identical geometry
    for both zone forms. Raises 400 if neither yields a usable polygon.
    """
    if body.polygon is not None and len(body.polygon) >= 3:
        return tuple(body.polygon)
    if body.road_path is not None and len(body.road_path) >= 2:
        buffer_m = body.buffer_m if body.buffer_m is not None else DEFAULT_ROAD_BUFFER_M
        polygon = buffer_path_to_polygon(tuple(body.road_path), buffer_m)
        if polygon is not None:
            return polygon
    raise HTTPException(400, "disruption zone could not be resolved to a polygon")


@router.post("/scenario/{scenario_id}/preview-disruption")
async def preview_scenario_disruption(
    scenario_id: str, body: PolygonDisruptionRequest, request: Request
):
    """Compute what a drawn disruption zone WOULD do, without committing it.

    Determines the affected vehicles/orders from the current live plan and
    returns a re-timed candidate route for each affected vehicle. Nothing is
    injected into the scenario clock and nothing is persisted — ``latest_plan``,
    ``state.plans``, and ``/map/state`` are all unchanged by calling this.
    """
    state = app_state(request)
    _validate_disruption_type(body.disruption_type)
    plan = _require_real_plan(state)

    polygon = _resolve_zone_polygon(body)
    affected_vehicle_ids, affected_order_ids = _affected_vehicle_and_order_ids(
        plan, state.orders, polygon
    )

    preview_event = DisruptionEvent(
        scenario_id=scenario_id,
        event_type=body.disruption_type,
        effective_minute=body.effective_minute,
        payload=_disruption_payload(body),
    )
    candidate = await _compute_candidate_plan(
        state,
        source_data_version=f"disruption-preview:{body.disruption_type}",
        parent_plan_id=state.active_plan_id,
        scenario_id=scenario_id,
        extra_disruptions=(preview_event,),
    )
    candidate_routes = filter_candidate_routes(candidate, affected_vehicle_ids)

    await state.record_audit(
        "SCENARIO_DISRUPTION_PREVIEWED",
        "dispatcher",
        {
            "scenario_id": scenario_id,
            "disruption_type": str(body.disruption_type),
            "affected_vehicle_ids": list(affected_vehicle_ids),
        },
    )
    return {
        "disruption_type": str(body.disruption_type),
        "affected": {"vehicle_ids": affected_vehicle_ids, "order_ids": affected_order_ids},
        "candidate_routes": [
            {
                "vehicle_id": route.vehicle_id,
                "geometry": route.geometry,
                "stops": route.stops,
            }
            for route in candidate_routes
        ],
    }


@router.post("/scenario/{scenario_id}/apply-disruption")
async def apply_scenario_disruption(
    scenario_id: str, body: PolygonDisruptionRequest, request: Request
):
    """Commit a drawn disruption zone: inject it and re-time the live plan.

    Unlike preview, this actually calls ``state.simulation.inject`` (so the
    zone becomes a real, persisted disruption event for this scenario) and
    updates ``latest_plan``'s timing via :func:`_apply_disruption_to_live_plan`
    — the same effect as ``POST /disruptions``, but built from a polygon zone
    plus type/severity instead of a raw ``DisruptionEvent`` body.
    """
    state = app_state(request)
    _validate_disruption_type(body.disruption_type)
    plan = _require_real_plan(state)

    polygon = _resolve_zone_polygon(body)
    affected_vehicle_ids, affected_order_ids = _affected_vehicle_and_order_ids(
        plan, state.orders, polygon
    )

    event = DisruptionEvent(
        scenario_id=scenario_id,
        event_type=body.disruption_type,
        effective_minute=body.effective_minute,
        payload=_disruption_payload(body),
    )
    try:
        injected = state.simulation.inject(event)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if state.persistence:
        await state.persistence.save_disruption(injected)
    await state.record_audit(
        "SCENARIO_DISRUPTION_APPLIED",
        "dispatcher",
        {
            "scenario_id": scenario_id,
            "event_id": injected.event_id,
            "disruption_type": str(body.disruption_type),
            "affected_vehicle_ids": list(affected_vehicle_ids),
        },
    )
    # A road closure proposes a mid-route reroute CANDIDATE (dotted suggestion
    # drawn on the scenario map only); otherwise the live plan is re-timed in
    # place. `reroute_candidate` is the proposed plan or null. The candidate is
    # scenario-only: it is never stored in shared state, so its full
    # routes+geometry are returned INLINE here for the scenario map to render —
    # the frontend does not (and cannot) fetch it from /plans/drafts.
    result = await _handle_disruption_effect(state, scenario_id, injected)
    candidate, rerouted_vehicle_ids = result if result is not None else (None, ())
    return {
        "disruption_type": str(body.disruption_type),
        "affected": {"vehicle_ids": affected_vehicle_ids, "order_ids": affected_order_ids},
        "event": injected,
        "reroute_candidate": (
            {
                "plan_id": candidate.plan_id,
                "version": candidate.version,
                # The vehicles that actually got a new route (subset of affected).
                # The frontend uses this to filter which routes from the candidate
                # plan to draw as the dotted suggestion — not the broader
                # affected set, which includes vehicles that fell back to re-time.
                "rerouted_vehicle_ids": list(rerouted_vehicle_ids),
                # The candidate's full routes (with geometry) inline, so the
                # scenario map can draw the dotted suggestion without the
                # candidate ever living in shared plan state.
                "routes": [route.model_dump(mode="json") for route in candidate.routes],
            }
            if candidate is not None
            else None
        ),
    }


@router.get("/map/state")
def map_state(request: Request, scenario_id: str = "demo") -> dict[str, Any]:
    state = app_state(request)
    clock = state.simulation.get(scenario_id)
    # The map shows the ACTIVE plan — the one the fleet is actually executing —
    # not the most recently generated candidate. A candidate the dispatcher has
    # not yet activated must never replace the running plan on screen; it only
    # takes over once activated via /plans/activate. When nothing is active yet
    # (e.g. a fresh from-scratch start), return an empty plan so the map renders
    # cleanly with no routes rather than surfacing an unapproved candidate.
    plan = state.active_plan
    if plan is None:
        return {
            "clock": clock,
            "trucks": [],
            "plan": {
                "plan_id": "",
                "version": 0,
                "status": "NO_ACTIVE_PLAN",
                "objective_cost": 0.0,
                "routes": [],
                "hard_violations": [],
            },
        }
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in state.fleet}
    minute = clock.current_minute
    trucks = [
        _truck_view(route, vehicle_by_id.get(route.vehicle_id), state.fleet[0], minute)
        for route in plan.routes
    ]
    return {"clock": clock, "trucks": trucks, "plan": plan}


def _truck_view(
    route: VehicleRoute,
    vehicle: Vehicle | None,
    fallback_vehicle: Vehicle,
    current_minute: int,
) -> dict[str, Any]:
    """One truck's map view: position, phase, and progress counts.

    Derives everything from the single shared :func:`vehicle_progress` so the
    map dot and the mid-route reroute seed can never disagree about where a
    truck is or which stops remain. When the route belongs to a vehicle not in
    the fleet map (defensive), ``fallback_vehicle`` supplies a depot origin.
    """
    progress = vehicle_progress(route, vehicle or fallback_vehicle, current_minute)
    return {
        "vehicle_id": route.vehicle_id,
        "driver_id": route.driver_id,
        "depot_id": vehicle.depot_id if vehicle else "",
        "position": progress.position,
        "phase": progress.phase,
        "completed_stops": progress.completed_count,
        "total_stops": len(route.stops),
    }


def _extract_driver_id(message: str, fleet: tuple[Vehicle, ...]) -> str | None:
    """Find a driver id named in the message, matched against the real fleet.

    Matches whitespace/punctuation-separated tokens in the message against the
    known ``driver_id`` values (case-insensitive), so it works for both
    ``DRV-01`` fixtures and numeric operational ids like ``452907`` without a
    brittle format guess or grabbing an unrelated number. Returns the canonical
    fleet id, or ``None`` when no known driver is referenced.
    """
    known = {vehicle.driver_id.casefold(): vehicle.driver_id for vehicle in fleet}
    if not known:
        return None
    for token in re.split(r"[^\w-]+", message):
        canonical = known.get(token.casefold())
        if canonical is not None:
            return canonical
    return None


def _extract_driver_id_with_history(
    message: str,
    fleet: tuple[Vehicle, ...],
    history: tuple[tuple[str, str], ...],
) -> str | None:
    """Extract a driver id from the current message OR recent conversation history.

    When the user says "562804" or "that driver" as a follow-up, the id may
    not be in the current message. We scan the most recent turns (newest first)
    so an id mentioned a couple of exchanges ago is still resolved correctly.
    """
    # Current message takes priority.
    driver_id = _extract_driver_id(message, fleet)
    if driver_id is not None:
        return driver_id
    # Walk recent history newest-first, stop at the first match.
    for _role, content in reversed(history):
        driver_id = _extract_driver_id(content, fleet)
        if driver_id is not None:
            return driver_id
    return None


def _classify_task_type_with_history(
    message: str,
    history: tuple[tuple[str, str], ...],
) -> str:
    """Classify the task type using the current message plus recent history.

    When a follow-up message ("562804", "all the drivers", "yes") is too short
    to classify on its own, look back through the last few user turns to find
    the most recent classifiable intent and inherit it. This lets a multi-turn
    dispatch conversation stay on track without the user repeating their intent
    every message.

    The current message always takes priority — only falls back to history when
    the current message produces CLARIFICATION_NEEDED.
    """
    task_type = classify_task_type(message)
    if task_type != "CLARIFICATION_NEEDED":
        return task_type

    # Scan the last 6 user turns (newest first) for a classifiable prior intent.
    user_turns = [
        content for role, content in reversed(history) if role == "user"
    ]
    for prior in user_turns[:6]:
        prior_type = classify_task_type(prior)
        if prior_type != "CLARIFICATION_NEEDED":
            return prior_type

    return "CLARIFICATION_NEEDED"


# --- LLM-chosen UI navigation ----------------------------------------------
# The LLM router (OpenAIGateway.route) decides, from the UNDERSTOOD INTENT of
# the message, which screen the dispatcher wants — returning one of the screen
# ids below (or "none"). This table only translates that already-decided screen
# id into the concrete frontend navigation directive; it does NOT match keywords
# against the message. New phrasings therefore never need a new entry here — the
# model handles paraphrase; this map only needs an entry per real screen.
_SCREEN_DIRECTIVES: dict[str, dict] = {
    "dispatcher_overview": {"action": "navigate", "path": "/dispatcher", "tab": "overview"},
    "plan_detail": {"action": "navigate", "path": "/dispatcher", "tab": "plan"},
    "draft_plans": {"action": "navigate", "path": "/dispatcher", "tab": "drafts"},
    "drivers": {"action": "navigate", "path": "/dispatcher", "tab": "enroll"},
    "orders": {"action": "navigate", "path": "/orders"},
    "scenario": {"action": "navigate", "path": "/scenario"},
    "operations": {"action": "navigate", "path": "/operations"},
}


def _directive_for_screen(screen: str | None) -> dict | None:
    """Translate an LLM-chosen screen id into a UI navigation directive.

    Returns ``None`` for ``"none"``/unknown/empty so no directive is emitted.
    Pure lookup — the intent decision was already made by the model.
    """
    if not screen or screen == "none":
        return None
    directive = _SCREEN_DIRECTIVES.get(screen)
    return dict(directive) if directive else None


async def _prepare_dispatch(state, body: DispatcherMessage):
    """Async pre-work shared by the plain and streaming dispatcher endpoints.

    Loads recent history, saves the user message, classifies the task, pre-fetches
    external context, and builds the AgentTask + AgentContext. Returns everything
    the graph run needs. Kept as one helper so the two endpoints cannot drift.
    """
    conversation_history: tuple[tuple[str, str], ...] = ()
    if state.persistence:
        try:
            recent = await state.persistence.recent_conversation_messages(
                body.conversation_id, limit=50
            )
            conversation_history = tuple((msg.role.lower(), msg.content) for msg in recent)
        except _PERSISTENCE_OUTAGE_ERRORS as exc:
            # DB outage mid-request: proceed with no prior history rather than
            # failing the turn. The reply loses continuity but still happens.
            logger.warning(
                "recent_conversation_messages failed for conversation=%s: %s; "
                "continuing with empty history",
                body.conversation_id,
                exc,
            )
        try:
            await state.persistence.save_conversation_message(
                ConversationMessage(
                    conversation_id=body.conversation_id,
                    role="USER",
                    content=body.message,
                    trust_label="HUMAN_DISPATCHER",
                    expires_at=datetime.now(UTC)
                    + timedelta(days=state.settings.conversation_retention_days),
                )
            )
        except _PERSISTENCE_OUTAGE_ERRORS as exc:
            # Best-effort save: losing this turn's history entry should not
            # block the dispatcher from getting a reply.
            logger.warning(
                "save_conversation_message (USER) failed for conversation=%s: %s; "
                "turn will proceed unsaved",
                body.conversation_id,
                exc,
            )
    task_type = _classify_task_type_with_history(body.message, conversation_history)
    task = AgentTask(
        task_type=task_type,
        requester="dispatcher",
        conversation_id=body.conversation_id,
        input_references=(f"message:{hashlib.sha256(body.message.encode()).hexdigest()[:12]}",),
        trust_labels=("HUMAN_DISPATCHER",),
    )
    demo_minute = state.simulation.get("demo").current_minute
    traffic_conditions = None
    weather_conditions = None
    if task_type == "DISRUPTION_ANALYSIS":
        traffic_conditions, weather_conditions = await asyncio.gather(
            summarize_traffic_conditions(state.lta),
            summarize_weather_conditions(state.nea),
        )
    context = AgentContext(
        plan=state.latest_plan,  # None when no plan exists yet — workers handle this gracefully
        fleet=state.fleet,
        orders=state.orders,
        max_stops_per_vehicle=state.settings.max_stops_per_vehicle,
        enforce_delivery_windows=state.settings.enforce_delivery_windows,
        active_disruptions=active_disruptions(state.simulation.events("demo"), demo_minute),
        traffic_conditions=traffic_conditions,
        weather_conditions=weather_conditions,
        driver_id=_extract_driver_id_with_history(body.message, state.fleet, conversation_history),
    )
    # Upfront curated-memory recall: prefer the hybrid (full-text + pgvector)
    # retriever backing the human /memory/search endpoint over the naive
    # in-memory keyword scan, since it ranks by both lexical and semantic
    # similarity. Falls back to the in-memory keyword scan when persistence
    # isn't configured (fixture mode / no DB), so recall still works offline.
    recalled_items: tuple = ()
    if state.persistence:
        try:
            query_embedding = await asyncio.to_thread(state.openai.embed, body.message)
            recalled_items = await state.persistence.search_curated_memory(
                body.message, query_embedding
            )
        except _PERSISTENCE_OUTAGE_ERRORS as exc:
            # DB/embedding outage: fall back to the in-memory keyword scan so
            # recall degrades to weaker matching instead of failing the turn.
            logger.warning(
                "search_curated_memory failed for conversation=%s: %s; "
                "falling back to in-memory keyword search",
                body.conversation_id,
                exc,
            )
            recalled_items = state.memory.search(body.message)
    else:
        recalled_items = state.memory.search(body.message)
    # POLICY items are always injected regardless of keyword/semantic match —
    # a standing policy (e.g. "never route through the CBD after 6pm") is
    # relevant to a turn even when the dispatcher's message contains nothing
    # that would surface it via search. Structured by_kind lookup, not fuzzy
    # recall. Deduplicated against whatever the recall pass above already
    # found so a matching POLICY item isn't sent to the router twice.
    always_injected_policies = state.memory.by_kind("POLICY")
    if len(always_injected_policies) > _POLICY_COUNT_WARN_THRESHOLD:
        logger.warning(
            "curated POLICY count (%d) exceeds %d; every dispatcher turn now "
            "injects that many policy snippets into the router prompt "
            "unconditionally — review for stale/superseded policies",
            len(always_injected_policies),
            _POLICY_COUNT_WARN_THRESHOLD,
        )
    recalled_ids = {item.memory_id for item in recalled_items}
    all_items = tuple(recalled_items) + tuple(
        item for item in always_injected_policies if item.memory_id not in recalled_ids
    )
    memory_snippets = tuple(item.content for item in all_items)
    # Transition the cross-turn agent state to ROUTING so the UI/operator can
    # see that a turn is in progress before the graph even starts.
    state.agent_state.start_turn(
        conversation_id=body.conversation_id,
        task_id=task.task_id,
        task_type=task_type,
    )
    state.agent_state.recall_count = len(memory_snippets)
    return task, task_type, context, memory_snippets, conversation_history


async def _persist_assistant_reply(state, body: DispatcherMessage, reply: str) -> None:
    """Persist an ASSISTANT turn so it shows up in later conversation history."""
    if state.persistence:
        try:
            await state.persistence.save_conversation_message(
                ConversationMessage(
                    conversation_id=body.conversation_id,
                    role="ASSISTANT",
                    content=reply,
                    trust_label="SYSTEM_GENERATED",
                    expires_at=datetime.now(UTC)
                    + timedelta(days=state.settings.conversation_retention_days),
                )
            )
        except _PERSISTENCE_OUTAGE_ERRORS as exc:
            # Best-effort save: the dispatcher already has the reply; losing
            # this turn's history entry should not surface as a failure.
            logger.warning(
                "save_conversation_message (ASSISTANT) failed for conversation=%s: %s; "
                "reply already sent, turn will remain unsaved",
                body.conversation_id,
                exc,
            )


async def _finalize_confirmation_reply(
    state, body: DispatcherMessage, task, reply: str, ui_directive: dict | None = None
) -> dict:
    """Response for a turn that resolved a pending confirmation (send or approval).

    Used when the dispatcher confirmed or cancelled a parked action, so this turn
    ran no workers of consequence — it just carries the outcome reply, persisted
    like any other assistant turn, in the same shape the endpoints expect. An
    optional ``ui_directive`` lets a resolved action also move the dispatcher
    (e.g. to the live map after activating a plan).
    """
    await _persist_assistant_reply(state, body, reply)
    return {
        "task": task,
        "result": None,
        "reply": reply,
        "generated_plan": None,
        "evidence_references": (),
        "ui_directive": ui_directive,
    }


def _prepare_driver_send_confirmation(
    state, body: DispatcherMessage, result, send_action: dict
) -> str:
    """Park a proposed single-driver send and return a confirmation prompt.

    Resolves the target driver (from the proposed action or the turn's computed
    metrics), checks the current plan actually has a route to send and that the
    driver is reachable on Telegram, and — when it does — stashes the intent on
    ``state.pending_driver_sends`` so the dispatcher's next "yes" actually sends.
    When there's nothing to send or the driver isn't reachable, it returns an
    explanatory reply and parks nothing, so a later "yes" can't fire a stale send.
    """
    metrics = result.computed_metrics if result is not None else {}
    driver_id = send_action.get("driver_id") or metrics.get("driver_id")
    if not driver_id:
        return (
            "I couldn't tell which driver you want the route sent to. Name the "
            "driver (for example, \"send DRV-01 their route\") and I'll confirm before sending."
        )

    plan = state.latest_plan
    if plan is None:
        state.pending_driver_sends.pop(body.conversation_id, None)
        return "No plan has been generated yet, so there's nothing to send."
    route = next((r for r in plan.routes if r.driver_id == driver_id), None)
    if route is None or not route.stops:
        state.pending_driver_sends.pop(body.conversation_id, None)
        return (
            f"Driver {driver_id} has no stops on the current plan, so there's nothing "
            "to send right now."
        )
    if state.enrollment.telegram_user_for(driver_id) is None:
        state.pending_driver_sends.pop(body.conversation_id, None)
        return (
            f"Driver {driver_id} isn't linked to a Telegram account (or is suspended), "
            "so I can't send their route. Enroll them under the Drivers tab first, then "
            "ask me again."
        )

    assigned_stops = len(route.stops)
    state.pending_driver_sends[body.conversation_id] = {
        "driver_id": driver_id,
        "vehicle_id": route.vehicle_id,
        "assigned_stops": assigned_stops,
    }
    def _clock(minute: int) -> str:
        return f"{minute // 60:02d}:{minute % 60:02d}"

    first_eta = _clock(route.stops[0].eta_minute)
    last_eta = _clock(route.stops[-1].eta_minute)
    return (
        f"Ready to send driver {driver_id} their route on vehicle {route.vehicle_id}: "
        f"{assigned_stops} stop(s), first ETA {first_eta}, last ETA {last_eta}. "
        "Reply \"yes\" to send it over Telegram, or \"no\" to hold off."
    )


def _current_draft_plans(state) -> list[PlanVersion]:
    """The draft (CANDIDATE/VALIDATED) plans awaiting a decision, newest first.

    Same selection as the /plans/drafts endpoint, reused so the agent resolves
    exactly the plans the dispatcher sees on the Draft Plans screen.
    """
    drafts = [
        versions[-1]
        for versions in state.plans.values()
        if versions and versions[-1].status in {"CANDIDATE", "VALIDATED"}
    ]
    drafts.sort(key=lambda plan: plan.created_at, reverse=True)
    return drafts


def _prepare_plan_approval_confirmation(state, body: DispatcherMessage) -> str:
    """Resolve the draft to activate, verify it's activatable, and park it.

    Mirrors the single-driver-send confirmation flow for plan activation: this
    only PROPOSES the activation and stashes it on ``state.pending_plan_approvals``;
    the dispatcher's next "yes" performs it (see the resolver at the top of
    ``_finalize_dispatch``). Parks nothing and returns an explanatory reply when
    there's no single unambiguous, activatable draft — so a later "yes" can't
    fire a stale or unsafe activation.
    """
    drafts = _current_draft_plans(state)
    if not drafts:
        state.pending_plan_approvals.pop(body.conversation_id, None)
        return (
            "There's no draft plan waiting for approval right now. Generate a plan "
            "first, then I can activate it for you."
        )
    if len(drafts) > 1:
        state.pending_plan_approvals.pop(body.conversation_id, None)
        listed = ", ".join(f"{p.plan_id[:8]} v{p.version}" for p in drafts[:5])
        return (
            f"There are {len(drafts)} draft plans awaiting review ({listed}). "
            "Tell me which one to activate (by its id), and I'll confirm before doing it."
        )

    plan = drafts[0]
    # Hard gate, identical to the activation endpoint's: a plan with hard
    # constraint violations can never go live. Decline instead of parking.
    if plan.hard_violations:
        state.pending_plan_approvals.pop(body.conversation_id, None)
        return (
            f"I can't activate draft {plan.plan_id[:8]} v{plan.version} — it has "
            f"{len(plan.hard_violations)} hard constraint violation(s) that must be "
            "resolved first. Try regenerating the plan."
        )

    state.pending_plan_approvals[body.conversation_id] = {
        "plan_id": plan.plan_id,
        "version": plan.version,
    }
    active_routes = sum(1 for r in plan.routes if r.stops)
    total_stops = sum(len(r.stops) for r in plan.routes)
    return (
        f"Ready to activate draft {plan.plan_id[:8]} v{plan.version}: "
        f"{active_routes} route(s), {total_stops} stop(s). Activating makes it the "
        "fleet's live plan and sends each driver their route. "
        "Reply \"yes\" to activate, or \"no\" to hold off."
    )


async def _finalize_dispatch(state, body: DispatcherMessage, task, task_type, final):
    """Async post-work shared by both endpoints: audit, plan, reply, persist.

    Takes the completed graph state (``final``) and produces the response dict.
    Identical for the plain and streaming endpoints so replies never diverge.
    """
    # A single-driver send the dispatcher was asked to confirm on a prior turn
    # takes priority: this turn is their answer ("yes send it" / "no cancel"),
    # not a fresh request, so it short-circuits the normal worker-result path.
    pending = state.pending_driver_sends.get(body.conversation_id)
    if pending is not None:
        if is_cancellation(body.message):
            state.pending_driver_sends.pop(body.conversation_id, None)
            await state.record_audit(
                "DRIVER_ROUTE_DISPATCH_CANCELLED",
                "dispatcher",
                {"driver_id": pending["driver_id"]},
            )
            return await _finalize_confirmation_reply(
                state,
                body,
                task,
                f"Okay, I won't send the route to driver {pending['driver_id']}. "
                "Nothing was sent.",
            )
        if is_confirmation(body.message):
            state.pending_driver_sends.pop(body.conversation_id, None)
            driver_id = pending["driver_id"]
            sent, reason = await _dispatch_route_to_driver(state, driver_id)
            if sent:
                reply = (
                    f"Sent driver {driver_id} their route over Telegram "
                    f"({pending.get('assigned_stops', 0)} stop(s))."
                )
            elif reason == "no_route":
                reply = (
                    f"Driver {driver_id} has no route on the current plan, so there "
                    "was nothing to send."
                )
            elif reason == "not_enrolled":
                reply = (
                    f"Driver {driver_id} isn't linked to a Telegram account (or is "
                    "suspended), so I couldn't send the route. Enroll them first, then "
                    "ask me again."
                )
            else:
                reply = (
                    f"I tried to send driver {driver_id} their route but the message "
                    "didn't go through. Please try again in a moment."
                )
            return await _finalize_confirmation_reply(state, body, task, reply)
        # A pending send exists but this message is neither a yes nor a no — fall
        # through to normal handling so the dispatcher can, say, ask a question;
        # the pending send stays parked until they confirm or cancel it.

    # A plan activation the agent proposed on a prior turn: this turn is the
    # dispatcher's yes/no. Same shape as the driver-send resolver above.
    pending_approval = state.pending_plan_approvals.get(body.conversation_id)
    if pending_approval is not None:
        if is_cancellation(body.message):
            state.pending_plan_approvals.pop(body.conversation_id, None)
            await state.record_audit(
                "PLAN_ACTIVATION_CANCELLED",
                "dispatcher",
                {"plan_id": pending_approval["plan_id"], "version": pending_approval["version"]},
            )
            return await _finalize_confirmation_reply(
                state, body, task,
                "Okay, I'll leave the draft as-is. Nothing was activated.",
            )
        if is_confirmation(body.message):
            state.pending_plan_approvals.pop(body.conversation_id, None)
            plan_id = pending_approval["plan_id"]
            version = pending_approval["version"]
            # Re-resolve the plan now (it may have changed since we parked it).
            plan = next(
                (p for p in state.plans.get(plan_id, []) if p.version == version), None
            )
            if plan is None:
                reply = (
                    "That draft is no longer available, so there was nothing to activate. "
                    "Generate a fresh plan and I'll activate it for you."
                )
            elif plan.status == "ACTIVE":
                reply = f"Draft {plan_id[:8]} v{version} is already the live plan."
            elif plan.hard_violations:
                # Re-check the hard gate at execution time, not just at park time.
                reply = (
                    f"I can't activate {plan_id[:8]} v{version} — it has "
                    f"{len(plan.hard_violations)} hard constraint violation(s). "
                    "Nothing was activated."
                )
            else:
                await _activate_plan_version(state, plan, dispatch=True)
                reply = (
                    f"Done — draft {plan_id[:8]} v{version} is now the live plan and each "
                    "driver has been sent their route. Taking you to the live map."
                )
                return await _finalize_confirmation_reply(
                    state,
                    body,
                    task,
                    reply,
                    ui_directive=_directive_for_screen("dispatcher_overview"),
                )
            return await _finalize_confirmation_reply(state, body, task, reply)
        # Neither yes nor no — leave the approval parked and handle normally.

    # A fresh "approve/activate the draft plan" request: propose it and park a
    # confirmation, mirroring the single-driver-send propose-then-confirm flow.
    # Only when nothing is already pending, so a yes/no above always wins.
    if (
        pending is None
        and pending_approval is None
        and is_plan_approval_request(body.message)
    ):
        reply = _prepare_plan_approval_confirmation(state, body)
        return await _finalize_confirmation_reply(state, body, task, reply)

    results = final.results or ([final.result] if final.result else [])

    for worker_result in results:
        await state.record_audit(
            "AGENT_TASK_COMPLETED",
            "master-dispatcher-agent",
            {"task_id": worker_result.task_id, "type": task_type, "status": worker_result.status},
        )
    for violation in final.contract_violations:
        await state.record_audit(
            "SKILL_VIOLATION",
            "skill-enforcer",
            {
                "node": violation.node,
                "action_type": violation.action_type,
                "severity": violation.severity,
                "reason": violation.reason,
            },
        )

    result = final.result or (results[-1] if results else None)
    # The turn's real evidence: the de-duplicated union of every worker's
    # evidence references. A conversational direct_reply (greeting, capability
    # question, clarification) runs no workers, so this stays empty — the client
    # must not claim plan/policy provenance for a reply that consulted neither.
    evidence_references: list[str] = []
    for worker_result in results:
        for ref in worker_result.evidence_references:
            if ref not in evidence_references:
                evidence_references.append(ref)
    statuses = {r.status for r in results}
    escalated = "ESCALATED" in statuses
    needs_input = bool(result) and result.status == "NEEDS_INPUT"
    proposed_types = {action.get("type") for r in results for action in r.proposed_actions}
    generated_plan = None
    if not escalated and proposed_types & {"GENERATE_CANDIDATE_PLAN", "BOUNDED_REPLAN"}:
        generated_plan = await _generate_and_store_plan(
            state,
            source_data_version=f"agent-triggered:{task_type.lower()}",
            parent_plan_id=state.active_plan_id,
            actor=f"{task_type.lower()}-agent",
            audit_event="AGENT_GENERATED_CANDIDATE_PLAN",
            audit_payload_extra={"task_id": task.task_id, "trigger": task_type},
        )

    # --- UI navigation directive ----------------------------------------------
    # A navigation directive is emitted ONLY when the dispatcher explicitly asked
    # to be taken somewhere on THIS turn. The LLM router captures that intent in
    # final.navigation (a screen id, or "none" for any message that wasn't a
    # navigation request). We never navigate as a side effect of another action:
    # generating a plan, analysing a disruption, or answering a driver question
    # leaves the dispatcher exactly where they are. If they then want to see the
    # result, they ask ("show me the draft") and that turn navigates.
    #
    # This deliberately drops the earlier auto-navigations (plan → drafts,
    # activate → overview): those moved the dispatcher without them asking.
    ui_directive: dict | None = None
    if not escalated:
        ui_directive = _directive_for_screen(getattr(final, "navigation", "none"))
    # --------------------------------------------------------------------------

    # A worker proposed sending one driver their route. The agent never sends on
    # the same turn it proposes: park the intent against this conversation and
    # ask the dispatcher to confirm. The actual send happens on the next turn,
    # via the pending-send short-circuit at the top of this function, only if the
    # dispatcher approves.
    send_action = next(
        (
            action
            for r in results
            for action in r.proposed_actions
            if action.get("type") == "SEND_DRIVER_ROUTE"
        ),
        None,
    )

    if final.direct_reply:
        reply = final.direct_reply
    elif needs_input:
        reply = result.escalation_reason or "Could you clarify what you need?"
    elif escalated:
        blocked = next((r for r in results if r.status == "ESCALATED"), None)
        reply = (
            (blocked.escalation_reason if blocked else None)
            or "That request was blocked because it crossed an agent's authority boundary."
        ) + " No action was taken."
    elif send_action is not None:
        reply = _prepare_driver_send_confirmation(state, body, result, send_action)
    elif task_type == "DRIVER_DISPATCH":
        # The dispatcher asked us to SEND something, but no send proposal survived
        # this turn (e.g. a capability contract dropped it). Answer deterministically
        # instead of handing read-only metrics to the LLM explainer — phrased freely,
        # it can narrate those metrics as though the dispatch already happened, which
        # would be a false claim that an action was performed.
        state.pending_driver_sends.pop(body.conversation_id, None)
        logger.warning(
            "DRIVER_DISPATCH turn produced no SEND_DRIVER_ROUTE action "
            "(conversation=%s, violations=%s)",
            body.conversation_id,
            [v.reason for v in (final.contract_violations or ())],
        )
        reply = (
            "I couldn't set up that send, so nothing was sent to the driver. "
            "Name the driver explicitly (for example, \"send 530633 their route\") "
            "and I'll confirm with you before sending anything."
        )
    else:
        candidate_plan_note = None
        if generated_plan is not None:
            candidate_plan_note = (
                f"A candidate plan (v{generated_plan.version}, id "
                f"{generated_plan.plan_id[:8]}, status {generated_plan.status}) was prepared for "
                "review but NOT activated. Offer to activate it: tell the dispatcher they can "
                "say \"approve the plan\" and you'll activate it after they confirm."
            )
        if result is not None and len(results) == 1:
            fallback = _explain_result(task_type, result.computed_metrics)
            if candidate_plan_note:
                fallback = f"{fallback} {candidate_plan_note}"
            reply = await asyncio.to_thread(
                state.openai.explain, task, result, fallback, candidate_plan_note
            )
        else:
            fallback = final.final_reply or "I reviewed the request across multiple checks."
            if candidate_plan_note:
                fallback = f"{fallback} {candidate_plan_note}"
            reply = await asyncio.to_thread(
                state.openai.explain_multi, task, results, fallback, candidate_plan_note
            )
    await _persist_assistant_reply(state, body, reply)
    # Commit any INCIDENT_LESSON proposals the graph produced this turn. The
    # graph runs on a worker-pool thread (see AgentSystem/write_back_from_result
    # in agents.py/memory.py) and deliberately never writes to MasterMemory
    # itself — it only returns candidate items on final.proposed_lessons — so
    # that MasterMemory stays single-writer: every insert happens here, on the
    # async layer, via add_items. This runs regardless of state.persistence so
    # fixture-mode/no-DB deployments still get write-back into the in-process
    # store, matching the pre-existing behavior before this call was split out.
    if final.proposed_lessons:
        state.memory.add_items(final.proposed_lessons)
    # Persist them too, when a database is configured, so they survive a
    # restart and can be recalled as the unvetted incident-lesson bucket on
    # future turns (see MasterMemory.recall_incident_lessons). Saved as-is
    # (PROPOSED / UNTRUSTED_EXTERNAL, no embedding).
    if state.persistence and final.proposed_lessons:
        try:
            for lesson in final.proposed_lessons:
                await state.persistence.save_memory(lesson)
        except _PERSISTENCE_OUTAGE_ERRORS as exc:
            # Best-effort save: the reply is already computed, and the lessons
            # are already in the in-process store above; losing the DB write
            # should not fail the response. They will simply not survive a
            # restart until a lesson is successfully persisted.
            logger.warning(
                "save_memory (INCIDENT_LESSON write-back) failed for "
                "conversation=%s: %s; proposed lessons not persisted this turn",
                body.conversation_id,
                exc,
            )

    # Transition the cross-turn agent state to DONE (or ERROR if escalated).
    final_phase = (
        TurnPhase.ERROR
        if "ESCALATED" in {r.status for r in (final.results or [])}
        else TurnPhase.DONE
    )
    state.agent_state.finish_turn(final_phase)
    # Reflect mid-turn recall count if the supervisor performed an enriched recall.
    if final.mid_turn_recall:
        state.agent_state.recall_count += len(final.mid_turn_recall)
    if ui_directive is not None and ui_directive.get("action") == "navigate":
        await state.record_audit(
            "AGENT_UI_NAVIGATION",
            "master-dispatcher-agent",
            {"path": ui_directive.get("path"), "tab": ui_directive.get("tab")},
        )
    return {
        "task": task,
        "result": result,
        "reply": reply,
        "generated_plan": generated_plan,
        "evidence_references": tuple(evidence_references),
        "ui_directive": ui_directive,
    }


@router.post("/dispatcher/messages")
async def dispatcher_message(body: DispatcherMessage, request: Request):
    state = app_state(request)
    task, task_type, context, memory_snippets, conversation_history = await _prepare_dispatch(
        state, body
    )
    # Run the bounded multi-worker loop. The raw message drives LLM planning; it
    # is deliberately not part of the audited AgentTask (whose input_references
    # hold only a hash of the message).
    final = state.agents.run(
        task,
        context,
        message=body.message,
        memory_snippets=memory_snippets,
        conversation_history=conversation_history,
    )
    return await _finalize_dispatch(state, body, task, task_type, final)


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/dispatcher/messages/stream")
async def dispatcher_message_stream(body: DispatcherMessage, request: Request):
    """Stream a dispatcher turn as Server-Sent Events with true per-node steps.

    Emits a ``step`` event as each graph node actually executes (routing, each
    worker, synthesize/respond), then a ``final`` event carrying the same
    response payload as :func:`dispatcher_message`. The graph runs synchronously
    in a worker thread; its step events are forwarded to this async generator
    through a queue so the client sees progress live. On any error a ``error``
    event is emitted so the client never hangs.
    """
    state = app_state(request)
    task, task_type, context, memory_snippets, conversation_history = await _prepare_dispatch(
        state, body
    )
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    def _drive() -> None:
        # Runs in a thread: drive the sync graph stream, pushing each event onto
        # the async queue via the event loop. The final GraphState is pushed as
        # the last item so the async side can finalize it.
        try:
            for kind, payload in state.agents.stream_run(
                task,
                context,
                message=body.message,
                memory_snippets=memory_snippets,
                conversation_history=conversation_history,
            ):
                loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))
        except Exception as exc:  # surface as an error event, never hang
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    async def _events():
        driver = asyncio.create_task(asyncio.to_thread(_drive))
        final_state = None
        error: str | None = None
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                kind, payload = item
                if kind == "step":
                    yield _sse("step", payload)
                elif kind == "final":
                    final_state = payload
                elif kind == "error":
                    error = payload
            if error is not None:
                yield _sse("error", {"message": "The request could not be completed."})
                return
            # Reuse the shared finalize path so the streamed reply is identical
            # to the non-streaming endpoint's.
            response = await _finalize_dispatch(state, body, task, task_type, final_state)
            yield _sse(
                "final",
                {
                    "reply": response["reply"],
                    "generated_plan": (
                        response["generated_plan"].model_dump(mode="json")
                        if response["generated_plan"] is not None
                        else None
                    ),
                    "evidence_references": list(response["evidence_references"]),
                    "ui_directive": response.get("ui_directive"),
                },
            )
        finally:
            await driver

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _explain_result(task_type: str, metrics: dict[str, Any]) -> str:
    if task_type == "ROUTE_PLAN":
        violations = metrics.get("hard_violations", 0)
        unassigned = metrics.get("unassigned_orders")
        detail = ""
        if unassigned is not None:
            detail = (
                f" {metrics.get('assigned_stops', 0)} stops are assigned across "
                f"{metrics.get('active_routes', 0)} active routes; {unassigned} order(s) "
                "remain unassigned within the current constraints."
            )
        return (
            "I reviewed the current plan. The validation trace reports "
            f"{violations} hard violations.{detail} Say \"approve the plan\" and I'll "
            "activate it after you confirm."
        )
    if task_type == "DISRUPTION_ANALYSIS":
        p90 = metrics.get("p90_finish_minutes")
        delta = metrics.get("p90_delta_minutes")
        overtime = metrics.get("overtime_probability")
        if p90 is not None:
            return (
                "I bounded the disruption impact with a Monte Carlo challenger: "
                f"p90 fleet finish is {p90} minutes ({delta:+} vs baseline), "
                f"overtime probability {overtime}. I proposed a replan; no plan was "
                "edited or activated."
            )
        return (
            "I bounded the disruption impact and proposed a replan. "
            "No plan was edited or activated."
        )
    stops = metrics.get("assigned_stops")
    if stops is not None and metrics.get("driver_id"):
        if stops:
            return (
                f"Driver {metrics['driver_id']} is assigned {stops} stop(s) on vehicle "
                f"{metrics.get('vehicle_id')}, first ETA {metrics.get('first_eta')} and last "
                f"{metrics.get('last_eta')}. I drafted a reply; it has not been sent."
            )
        return (
            f"Driver {metrics['driver_id']} has no stops on the current plan. "
            "I drafted a reply; it has not been sent."
        )
    return "I drafted a driver response. It has not been sent."


@router.post("/approvals")
async def create_approval(body: ApprovalCreate, request: Request):
    state = app_state(request)
    get_plan(body.plan_id, body.plan_version, request)
    approval = state.approvals.create(body.plan_id, body.plan_version, body.action)
    if state.persistence:
        await state.persistence.save_approval(approval)
    await state.record_audit(
        "APPROVAL_REQUESTED", "policy-engine", {"approval_id": approval.approval_id}
    )
    return approval


@router.get("/approvals/{approval_id}")
def approval_status(approval_id: str, request: Request):
    try:
        return app_state(request).approvals.requests[approval_id]
    except KeyError as exc:
        raise HTTPException(404, "approval not found") from exc


@router.post("/approvals/{approval_id}/demo-proof")
def demo_proof(approval_id: str, request: Request):
    try:
        return app_state(request).approvals.demo_proof(approval_id)
    except KeyError as exc:
        raise HTTPException(404, "approval not found") from exc


@router.post("/approvals/{approval_id}/proof")
async def submit_proof(approval_id: str, body: ProofSubmission, request: Request):
    try:
        state = app_state(request)
        result = state.approvals.verify_and_execute(approval_id, body.proof)
    except KeyError as exc:
        raise HTTPException(404, "approval not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    if state.persistence:
        await state.persistence.save_approval(result, str(body.proof.get("nonce", "")))
    await state.record_audit(
        "APPROVAL_EXECUTED", "x401-verifier", {"approval_id": approval_id}
    )
    return result


@router.post("/telegram/enrollment-tokens")
async def issue_enrollment(body: EnrollmentIssue, request: Request):
    state = app_state(request)
    token, digest, expires_at = state.enrollment.issue(body.driver_id)
    if state.persistence:
        await state.persistence.save_telegram_enrollment(body.driver_id, digest, expires_at)
    return {"token": token, "expires_in_seconds": 600}


@router.get("/telegram/drivers")
def list_telegram_drivers(request: Request) -> list[dict[str, Any]]:
    """List drivers currently linked to a Telegram account.

    Each record carries the bound ``telegram_user_id`` and whether the link is
    ``suspended`` (bound but muted). Drivers that were never enrolled are simply
    absent from this list.
    """
    state = app_state(request)
    return list(state.enrollment.linked_drivers())


@router.post("/telegram/drivers/{driver_id}/suspend")
async def suspend_driver(driver_id: str, request: Request):
    state = app_state(request)
    state.enrollment.suspend(driver_id)
    if state.persistence:
        await state.persistence.suspend_telegram_driver(driver_id)
    await state.record_audit("DRIVER_SUSPENDED", "dispatcher", {"driver_id": driver_id})
    return {"driver_id": driver_id, "status": "SUSPENDED"}


@router.post("/telegram/drivers/{driver_id}/reactivate")
async def reactivate_driver(driver_id: str, request: Request):
    """Lift a driver's suspension so they receive route messages again."""
    state = app_state(request)
    reactivated = state.enrollment.reactivate(driver_id)
    if state.persistence:
        await state.persistence.reactivate_telegram_driver(driver_id)
    await state.record_audit("DRIVER_REACTIVATED", "dispatcher", {"driver_id": driver_id})
    return {"driver_id": driver_id, "status": "ACTIVE", "was_suspended": reactivated}


@router.post("/telegram/drivers/{driver_id}/unlink")
async def unlink_driver(driver_id: str, request: Request):
    """Remove a driver's Telegram binding so the id can be enrolled again."""
    state = app_state(request)
    removed = state.enrollment.unlink(driver_id)
    if state.persistence:
        await state.persistence.unlink_telegram_driver(driver_id)
    await state.record_audit("DRIVER_UNLINKED", "dispatcher", {"driver_id": driver_id})
    return {"driver_id": driver_id, "status": "UNLINKED", "was_linked": removed}


@public_router.post("/telegram/webhook")
async def telegram_webhook(
    payload: dict[str, Any],
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    state = app_state(request)
    if x_telegram_bot_api_secret_token != state.settings.telegram_webhook_secret:
        raise HTTPException(403, "invalid Telegram webhook secret")
    message = payload.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    text = str(message.get("text", ""))
    if chat.get("type") != "private":
        raise HTTPException(403, "private chats only")
    if text.startswith("/start enroll_"):
        token = text.removeprefix("/start enroll_")
        try:
            telegram_user_id = int(sender["id"])
            driver = state.enrollment.enroll(token, telegram_user_id, str(chat["type"]))
        except (KeyError, PermissionError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        if state.persistence:
            await state.persistence.bind_telegram_driver(driver, telegram_user_id)
        await state.record_audit(
            "DRIVER_ENROLLED", "telegram", {"driver_id": driver}
        )
        return {"ok": True, "driver_id": driver, "message_sent": False}
    driver = state.enrollment.driver_for(int(sender.get("id", -1)))
    if not driver:
        # Log the chat id so a dispatcher can discover their own chat id (for
        # DISPATCHER_TELEGRAM_CHAT_ID) simply by messaging the bot and reading
        # the API logs. Harmless for real drivers — they're enrolled and never
        # hit this branch.
        logging.getLogger(__name__).info(
            "Unenrolled Telegram message from chat id %s", sender.get("id")
        )
        raise HTTPException(403, "Telegram identity is not enrolled")

    # Run the driver-facing agent and send its reply back on Telegram.
    telegram_user_id = int(sender.get("id", -1))
    agent_result = await asyncio.to_thread(
        state.agents.handle_driver_message,
        text,
        driver,
        state.latest_plan,
        state.fleet,
        state.orders,
    )

    # When the agent decided the driver wants their full schedule, send the
    # full HTML route message (same as plan activation dispatch) first, then
    # the agent's short plain-text confirmation after it.
    route_sent = False
    if agent_result.get("send_full_route"):
        plan = state.latest_plan
        route = next(
            (r for r in (plan.routes if plan else []) if r.driver_id == driver),
            None,
        )
        if route and plan:
            route_sent = await send_route_messages(
                state.telegram,
                telegram_user_id,
                route,
                state.orders,
                plan,
                state.fleet,
                depot=next(
                    (v.start for v in state.fleet if v.vehicle_id == route.vehicle_id),
                    None,
                ),
            )

    reply_text = format_driver_reply(agent_result["reply"])
    message_sent = await state.telegram.send_message(telegram_user_id, reply_text)

    breakdown = agent_result.get("breakdown_report")
    if breakdown:
        await state.record_audit(
            "DRIVER_BREAKDOWN_REPORTED",
            "telegram",
            {
                "driver_id": driver,
                "vehicle_id": breakdown.get("vehicle_id"),
                "description": breakdown.get("description"),
                "location": breakdown.get("location"),
                "remaining_stops": breakdown.get("remaining_stops"),
            },
        )
        # Push a live in-app notification to every connected dispatcher UI so a
        # breakdown pops up immediately, not just in the audit log. Best-effort:
        # no connected client simply means nobody's watching right now.
        await state.notifications.publish(
            {
                "type": "DRIVER_BREAKDOWN",
                "driver_id": driver,
                "vehicle_id": breakdown.get("vehicle_id"),
                "description": breakdown.get("description"),
                "location": breakdown.get("location"),
                "remaining_stops": breakdown.get("remaining_stops"),
                "at": datetime.now(UTC).isoformat(),
            }
        )

    return {
        "ok": True,
        "driver_id": driver,
        "reply": reply_text,
        "message_sent": message_sent,
        "route_sent": route_sent,
        "breakdown_reported": breakdown is not None,
    }


@router.post("/memory/proposals")
async def propose_memory(body: MemoryProposal, request: Request):
    try:
        state = app_state(request)
        item = state.memory.propose(body.kind, body.content, body.trust_label)
        if state.persistence:
            await state.persistence.save_memory(item)
        return item
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/memory/proposals")
def proposed_memory(request: Request):
    return app_state(request).memory.proposed()


@router.post("/memory/{memory_id}/curate")
async def curate_memory(memory_id: str, request: Request):
    try:
        state = app_state(request)
        item = state.memory.curate(memory_id)
        if state.persistence:
            embedding = await asyncio.to_thread(state.openai.embed, item.content)
            await state.persistence.save_memory(item, embedding)
        return item
    except KeyError as exc:
        raise HTTPException(404, "memory not found") from exc


@router.post("/memory/{memory_id}/supersede")
async def supersede_memory(memory_id: str, body: MemoryReplacement, request: Request):
    try:
        state = app_state(request)
        item = state.memory.supersede(memory_id, body.content)
        if state.persistence:
            await state.persistence.save_memory(state.memory.get(memory_id))
            embedding = await asyncio.to_thread(state.openai.embed, item.content)
            await state.persistence.save_memory(item, embedding)
        return item
    except KeyError as exc:
        raise HTTPException(404, "memory not found") from exc


@router.get("/memory/search")
async def search_memory(request: Request, q: str):
    state = app_state(request)
    if not state.persistence:
        return state.memory.search(q)
    embedding = await asyncio.to_thread(state.openai.embed, q)
    return await state.persistence.search_curated_memory(q, embedding)


@router.get("/memory/conversations/search")
async def search_conversations(request: Request, q: str):
    state = app_state(request)
    if not state.persistence:
        return []
    return await state.persistence.search_conversations(q)


@router.delete("/dispatcher/conversations/{conversation_id}")
async def clear_conversation(conversation_id: str, request: Request):
    """Delete all persisted messages for a conversation.

    Called by the frontend clear-chat button so the dispatcher can start
    fresh without the agent carrying stale context from a prior session.
    """
    state = app_state(request)
    deleted = 0
    if state.persistence:
        deleted = await state.persistence.delete_conversation(conversation_id)
    await state.record_audit(
        "CONVERSATION_CLEARED",
        "dispatcher",
        {"conversation_id": conversation_id, "messages_deleted": deleted},
    )
    return {"conversation_id": conversation_id, "messages_deleted": deleted}


@router.get("/dispatcher/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str, request: Request, limit: int = 100
):
    """Return the most recent messages for a conversation, oldest first.

    Used by the frontend to seed its in-memory chat history after a page
    refresh so the dispatcher never loses context between sessions.
    """
    state = app_state(request)
    if not state.persistence:
        return []
    messages = await state.persistence.recent_conversation_messages(
        conversation_id, limit=min(limit, 200)
    )
    # recent_conversation_messages already returns messages oldest-first
    # (the repository reverses the DESC query internally); no second reversal needed.
    return [
        {
            "role": msg.role.lower(),     # "user" or "assistant"
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
        }
        for msg in messages
    ]


@router.get("/operations/integrations")
async def integration_health(request: Request, live: bool = False):
    state = app_state(request)
    if not live:
        configured = [
            {
                "integration": "OneMap",
                "status": "CONFIGURED" if state.settings.onemap_access_token else "NOT_CONFIGURED",
            },
            {
                "integration": "LTA DataMall",
                "status": "CONFIGURED"
                if state.settings.lta_datamall_account_key
                else "NOT_CONFIGURED",
            },
            {"integration": "NEA/data.gov.sg", "status": "CONFIGURED"},
            {
                "integration": "OpenAI",
                "status": "CONFIGURED" if state.settings.openai_api_key else "NOT_CONFIGURED",
            },
        ]
        if not state.repository:
            return configured
        stale_after = {
            ("LTA", "incidents"): state.settings.lta_incidents_stale_seconds,
            ("LTA", "vms"): state.settings.lta_incidents_stale_seconds,
            ("LTA", "speed_bands"): state.settings.lta_traffic_stale_seconds,
            ("LTA", "travel_times"): state.settings.lta_traffic_stale_seconds,
            ("NEA", "rainfall"): state.settings.nea_rainfall_stale_seconds,
            ("NEA", "two_hour_forecast"): state.settings.nea_forecast_stale_seconds,
        }
        snapshots = []
        for snapshot in await state.repository.latest_integration_snapshots():
            age = (datetime.now(UTC) - snapshot["fetched_at"]).total_seconds()
            status = snapshot["status"]
            limit = stale_after.get((snapshot["integration"], snapshot["dataset"]), 900)
            if status == "FRESH" and age > limit:
                status = "STALE"
            snapshots.append(
                {
                    "integration": f"{snapshot['integration']} {snapshot['dataset']}",
                    "status": status,
                    "fetched_at": snapshot["fetched_at"],
                    "age_seconds": round(age),
                    "record_count": snapshot["record_count"],
                }
            )
        return configured + snapshots
    onemap, rainfall, forecast = await asyncio.gather(
        state.onemap.health_check(),
        state.nea.collect("rainfall"),
        state.nea.collect("two_hour_forecast"),
    )
    lta_status = SnapshotStatus.NOT_CONFIGURED
    if state.settings.lta_datamall_account_key:
        lta_status = (await state.lta.collect("incidents")).status
    return [
        onemap,
        {"integration": "LTA DataMall", "status": lta_status},
        {"integration": "NEA rainfall", "status": rainfall.status},
        {"integration": "NEA two-hour forecast", "status": forecast.status},
    ]


@router.get("/operations/forecast-gate")
async def forecast_gate(request: Request):
    state = app_state(request)
    synchronized_days = (
        await state.repository.synchronized_weather_traffic_days() if state.repository else 0
    )
    return {
        "status": "EXPERIMENTAL",
        "synchronized_days": synchronized_days,
        "required_days": 14,
        "eligible_for_evaluation": synchronized_days >= 14,
        "operational": False,
    }


@router.post("/operations/integrations/onemap/recheck")
async def recheck_onemap(request: Request):
    state = app_state(request)
    state.onemap.reset_authentication_failure()
    return await state.onemap.health_check()


@router.get("/operations/audit")
def audit_events(request: Request):
    state = app_state(request)
    return {"valid": state.audit.verify(), "events": state.audit.events}


@router.post("/evaluations/run")
def run_evaluation(request: Request):
    return {
        "run_at": datetime.now(UTC),
        **evaluate_scenarios(app_state(request).settings.monte_carlo_eval_samples),
    }


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket):
    await websocket.accept()
    state = app_state(websocket)
    queue = state.notifications.subscribe()
    try:
        while True:
            # Wait for a published notification, but wake up every 5s regardless
            # to send a heartbeat (keeps the connection alive and lets us detect
            # a dropped client promptly).
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5)
                await websocket.send_json(event)
            except TimeoutError:
                await websocket.send_json(
                    {"type": "heartbeat", "at": datetime.now(UTC).isoformat()}
                )
    except WebSocketDisconnect:
        return
    finally:
        state.notifications.unsubscribe(queue)


# --- Hands-free voice console WebSocket --------------------------------------
# The browser owns turn-taking and barge-in (client-authoritative): it does
# voice-activity detection locally, sends ONE finalized utterance blob per turn,
# and — the instant the user speaks over a spoken reply — cuts its own audio and
# sends {"type":"interrupt"}. This handler transcribes the utterance, runs the
# SAME agent graph the text chat uses, and streams the spoken reply back
# sentence-by-sentence, cancelling immediately on interrupt.
#
# Message schema:
#   C -> S:  binary frame                 = a finalized utterance (audio blob)
#            {"type": "interrupt"}         = user barged in; stop speaking now
#            {"type": "mime", "value": ..} = (optional) the recorder's mime type
#   S -> C:  {"type": "transcript", "role": "user"|"agent", "text": ...}
#            binary frame                  = a TTS audio chunk (one sentence)
#            {"type": "tts_end"}           = reply finished speaking
#            {"type": "error", "message": ...}

VOICE_CONVERSATION_ID = "global-dispatcher-ui"


def _voice_authenticated(websocket: WebSocket) -> bool:
    """Verify the dispatcher session cookie on a WebSocket handshake.

    WebSocket routes are registered outside the HTTP `require_admin` dependency,
    so the session cookie must be checked here explicitly. Mirrors require_admin.
    """
    settings = app_state(websocket).settings
    token = websocket.cookies.get(SESSION_COOKIE_NAME, "")
    return verify_session_token(token, settings.app_session_secret) is not None


async def _voice_run_turn(state, websocket: WebSocket, transcript: str) -> None:
    """Transcribe -> agent -> streamed TTS for one voice turn.

    Runs the exact dispatch path the text chat uses (_prepare_dispatch ->
    agents.run -> _finalize_dispatch) so voice turns share logic, history, and
    audit trail. Raises asyncio.CancelledError cleanly when the caller cancels
    this task on a barge-in — the finally clause guarantees a tts_end is sent.
    """
    body = DispatcherMessage(conversation_id=VOICE_CONVERSATION_ID, message=transcript)
    # Echo the recognized user text so the UI thread shows the spoken turn.
    await websocket.send_json({"type": "transcript", "role": "user", "text": transcript})

    task, task_type, context, memory_snippets, conversation_history = await _prepare_dispatch(
        state, body
    )
    # The graph is synchronous; run it off the event loop so the socket stays
    # responsive to an incoming interrupt while the agent is thinking.
    final = await asyncio.to_thread(
        state.agents.run,
        task,
        context,
        message=body.message,
        memory_snippets=memory_snippets,
        conversation_history=conversation_history,
    )
    response = await _finalize_dispatch(state, body, task, task_type, final)
    reply = response["reply"]

    # Surface the agent reply as a thread message, then speak it.
    await websocket.send_json({"type": "transcript", "role": "agent", "text": reply})

    # Forward any navigation/refresh directive so a spoken "go to X" actually
    # moves the dispatcher, matching the text chat's behavior. Sent before TTS
    # so the page change happens while the reply is being spoken.
    directive = response.get("ui_directive")
    if directive is not None:
        await websocket.send_json({"type": "directive", "directive": directive})

    try:
        async for chunk in state.voice.stream_tts(reply):
            await websocket.send_bytes(chunk)
    finally:
        # Always tell the client the spoken reply is done (even on cancel), so
        # it can flip back to LISTENING deterministically.
        try:
            await websocket.send_json({"type": "tts_end"})
        except RuntimeError:
            pass  # socket already closing


@router.websocket("/ws/voice")
async def voice_socket(websocket: WebSocket):
    await websocket.accept()
    if not _voice_authenticated(websocket):
        await websocket.close(code=4401)  # application-level "unauthorized"
        return

    state = app_state(websocket)
    turn_task: asyncio.Task | None = None

    def cancel_turn() -> None:
        nonlocal turn_task
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        turn_task = None

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            # A binary frame is one finalized utterance → start a turn.
            if (blob := message.get("bytes")) is not None:
                # A new utterance supersedes any still-speaking reply.
                cancel_turn()

                async def _run(audio: bytes) -> None:
                    try:
                        transcript = await state.voice.transcribe(audio)
                        if not transcript:
                            await websocket.send_json({"type": "tts_end"})
                            return
                        await _voice_run_turn(state, websocket, transcript)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # never let one turn kill the socket
                        logger.exception("voice turn failed")
                        try:
                            await websocket.send_json(
                                {"type": "error", "message": "That didn't go through — try again."}
                            )
                        except RuntimeError:
                            pass

                turn_task = asyncio.create_task(_run(blob))
                continue

            # A text frame is a control message.
            if (text := message.get("text")) is not None:
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "interrupt":
                    # Client-authoritative barge-in: it has already cut its own
                    # audio; we just stop synthesizing/sending immediately.
                    cancel_turn()
    except WebSocketDisconnect:
        pass
    finally:
        cancel_turn()
