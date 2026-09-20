from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from .agents import AgentContext, classify_task_type
from .auth import (
    SESSION_COOKIE_NAME,
    issue_session_token,
    verify_password,
    verify_session_token,
)
from .disruptions import active_disruptions, disruption_speed_kph_by_stop
from .dispatch import format_route_message
from .domain import (
    AgentTask,
    ConversationMessage,
    Coordinate,
    DisruptionEvent,
    PlanVersion,
    PolicyInput,
    SnapshotStatus,
)
from .evaluation import run_evaluation as evaluate_scenarios
from .planning import (
    assign_orders_to_depots,
    build_plan,
    depot_node_id,
    greedy_baseline,
    plan_delta,
    reapply_route_timing,
    validate_plan,
)
from .policy import decide_policy
from .route_geometry import enrich_plan_geometry, road_distance_matrix
from .simulation import interpolated_progress, point_along_geometry

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


class ClockUpdate(BaseModel):
    playing: bool | None = None
    speed: int | None = None
    current_minute: int | None = None


class BranchRequest(BaseModel):
    at_minute: int = Field(ge=0, le=1439)


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
        "data_source": state.settings.data_source,
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
    # Synthetic mode has no standalone driver records; surface the ids in use.
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
    scenario_events = state.simulation.events(scenario_id)
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
    )
    if state.settings.onemap_access_token:
        depot_by_vehicle = {vehicle.vehicle_id: vehicle.start for vehicle in state.fleet}
        plan = await enrich_plan_geometry(
            plan, state.fleet[0].start, state.onemap, depot_by_vehicle
        )
    state.plans[plan.plan_id].append(plan)
    await state.save_plan(plan)
    payload = {"plan_id": plan.plan_id, "version": plan.version, **(audit_payload_extra or {})}
    await state.record_audit(audit_event, actor, payload)
    return plan


@router.post("/plans/generate")
async def generate_plan(body: GeneratePlanRequest, request: Request):
    state = app_state(request)
    return await _generate_and_store_plan(
        state,
        source_data_version=body.source_data_version,
        parent_plan_id=body.parent_plan_id,
        actor="dispatcher",
        audit_event="PLAN_GENERATED",
    )


@router.get("/plans")
def list_plans(request: Request):
    state = app_state(request)
    return [version for versions in state.plans.values() for version in versions]


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
        message = format_route_message(route, state.orders, plan, state.fleet)
        sent[route.driver_id] = await state.telegram.send_message(
            telegram_user_id, message, parse_mode="HTML"
        )
    return sent


@router.post("/plans/activate")
async def activate_plan(body: ActivatePlanRequest, request: Request):
    state = app_state(request)
    if state.settings.app_env == "test" and not state.settings.allow_plan_execution_in_tests:
        raise HTTPException(403, "plan execution is disabled in tests")
    plan = get_plan(body.plan_id, body.version, request)
    if plan.hard_violations:
        raise HTTPException(409, "a plan with hard violations cannot be activated")
    state.active_plan_id = plan.plan_id
    activated = plan.model_copy(update={"status": "ACTIVE"})
    versions = state.plans[plan.plan_id]
    versions[versions.index(plan)] = activated
    await state.save_plan(activated)
    await state.record_audit(
        "PLAN_ACTIVATED", "dispatcher", {"plan_id": plan.plan_id, "version": plan.version}
    )
    dispatched = await _dispatch_plan_to_drivers(state, activated)
    await state.record_audit(
        "PLAN_DISPATCHED_TO_DRIVERS",
        "dispatcher",
        {"plan_id": plan.plan_id, "version": plan.version, "sent": dispatched},
    )
    return activated


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
    await _apply_disruption_to_live_plan(state, event.scenario_id)
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
    active_tag = ",".join(sorted({str(e.event_type) for e in active}))
    if plan.source_data_version.startswith("disruption-timing:"):
        applied_tag = plan.source_data_version.split(":", 1)[1].split("+", 1)[0]
        if applied_tag == active_tag:
            # Already reflects exactly this set of disruption types; the clock
            # advancing further within the same disruption is not a new event.
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


@router.get("/map/state")
def map_state(request: Request, scenario_id: str = "demo") -> dict[str, Any]:
    state = app_state(request)
    clock = state.simulation.get(scenario_id)
    plan = state.latest_plan
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in state.fleet}
    trucks = []
    for route in plan.routes:
        vehicle = vehicle_by_id.get(route.vehicle_id)
        depot_position = vehicle.start if vehicle else state.fleet[0].start
        position = depot_position
        # A route with no stops is a vehicle held in reserve for this plan.
        phase = "STANDBY" if not route.stops else "AT_DEPOT"
        for stop in route.stops:
            if clock.current_minute >= stop.eta_minute:
                position = stop.location
                phase = "SERVICING" if clock.current_minute < stop.departure_minute else "EN_ROUTE"
                continue
            previous_departure = vehicle.working_start_minute if vehicle else 480
            previous_position = depot_position
            prior_stops = [prior for prior in route.stops if prior.sequence < stop.sequence]
            if prior_stops:
                previous = prior_stops[-1]
                previous_departure = previous.departure_minute
                previous_position = previous.location
            progress = interpolated_progress(
                clock.current_minute, previous_departure, stop.eta_minute
            )
            position = previous_position.model_copy(
                update={
                    "lat": previous_position.lat
                    + (stop.location.lat - previous_position.lat) * progress,
                    "lon": previous_position.lon
                    + (stop.location.lon - previous_position.lon) * progress,
                }
            )
            phase = "EN_ROUTE"
            break
        trucks.append(
            {
                "vehicle_id": route.vehicle_id,
                "driver_id": route.driver_id,
                "depot_id": vehicle.depot_id if vehicle else "",
                "position": position,
                "phase": phase,
                "completed_stops": sum(
                    clock.current_minute >= stop.departure_minute for stop in route.stops
                ),
                "total_stops": len(route.stops),
            }
        )
        if route.geometry:
            trucks[-1]["position"] = point_along_geometry(
                route.geometry,
                interpolated_progress(
                    clock.current_minute,
                    480,
                    480 + max(1, route.duration_minutes),
                ),
            )
    return {"clock": clock, "trucks": trucks, "plan": plan}


@router.post("/dispatcher/messages")
async def dispatcher_message(body: DispatcherMessage, request: Request):
    state = app_state(request)
    if state.persistence:
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
    # The master agent owns message classification; the API defers to it rather
    # than maintaining a second, drift-prone classifier.
    task_type = classify_task_type(body.message)
    task = AgentTask(
        task_type=task_type,
        requester="dispatcher",
        conversation_id=body.conversation_id,
        input_references=(f"message:{hashlib.sha256(body.message.encode()).hexdigest()[:12]}",),
        trust_labels=("HUMAN_DISPATCHER",),
    )
    demo_minute = state.simulation.get("demo").current_minute
    context = AgentContext(
        plan=state.latest_plan,
        fleet=state.fleet,
        orders=state.orders,
        max_stops_per_vehicle=state.settings.max_stops_per_vehicle,
        enforce_delivery_windows=state.settings.enforce_delivery_windows,
        active_disruptions=active_disruptions(state.simulation.events("demo"), demo_minute),
    )
    result = state.agents.invoke(task, context)
    await state.record_audit(
        "AGENT_TASK_COMPLETED",
        "master-dispatcher-agent",
        {"task_id": task.task_id, "type": task_type, "status": result.status},
    )
    generated_plan = None
    proposed_types = {action.get("type") for action in result.proposed_actions}
    if result.status == "COMPLETED" and proposed_types & {
        "GENERATE_CANDIDATE_PLAN",
        "BOUNDED_REPLAN",
    }:
        # The agent may compute a new candidate plan on its own — this is pure
        # computation (build_plan + validation), never activation. The result
        # sits alongside the current plan until a human calls /plans/activate;
        # nothing here changes what the fleet is actually executing.
        generated_plan = await _generate_and_store_plan(
            state,
            source_data_version=f"agent-triggered:{task_type.lower()}",
            parent_plan_id=state.active_plan_id,
            actor=f"{task_type.lower()}-agent",
            audit_event="AGENT_GENERATED_CANDIDATE_PLAN",
            audit_payload_extra={"task_id": task.task_id, "trigger": task_type},
        )
    if result.status == "NEEDS_INPUT":
        # The master could not classify the request; ask the dispatcher to
        # clarify directly rather than routing to a worker or an LLM explainer.
        reply = result.escalation_reason or "Could you clarify what you need?"
    else:
        fallback = _explain_result(task_type, result.computed_metrics)
        reply = await asyncio.to_thread(state.openai.explain, task, result, fallback)
        if generated_plan is not None:
            reply += (
                f" I generated candidate plan v{generated_plan.version} "
                f"({generated_plan.plan_id[:8]}, status {generated_plan.status}) for review; "
                "it has not been activated."
            )
    if state.persistence:
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
    return {
        "task": task,
        "result": result,
        "reply": reply,
        "generated_plan": generated_plan,
    }


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
            f"{violations} hard violations.{detail} Activation remains policy-controlled."
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


@router.post("/telegram/drivers/{driver_id}/suspend")
async def suspend_driver(driver_id: str, request: Request):
    state = app_state(request)
    state.enrollment.suspend(driver_id)
    if state.persistence:
        await state.persistence.suspend_telegram_driver(driver_id)
    await state.record_audit("DRIVER_SUSPENDED", "dispatcher", {"driver_id": driver_id})
    return {"driver_id": driver_id, "status": "SUSPENDED"}


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
        raise HTTPException(403, "Telegram identity is not enrolled")
    assigned = next(
        (route for route in state.latest_plan.routes if route.driver_id == driver), None
    )
    return {"ok": True, "driver_id": driver, "assignment": assigned, "message_sent": False}


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
    try:
        while True:
            await websocket.send_json({"type": "heartbeat", "at": datetime.now(UTC).isoformat()})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return
