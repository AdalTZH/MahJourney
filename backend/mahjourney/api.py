from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .domain import AgentTask, ConversationMessage, DisruptionEvent, PolicyInput, SnapshotStatus
from .evaluation import run_evaluation as evaluate_scenarios
from .planning import build_plan, greedy_baseline, plan_delta, validate_plan
from .policy import decide_policy
from .route_geometry import enrich_plan_geometry
from .simulation import interpolated_progress, point_along_geometry

router = APIRouter(prefix="/api/v1")


def app_state(request: Request):
    return request.app.state.services


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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mahjourney-api"}


@router.get("/ready")
def ready(request: Request) -> dict[str, Any]:
    state = app_state(request)
    return {
        "status": "ready",
        "mode": "fixture-safe" if state.settings.missing_live_credentials() else "live-capable",
        "missing_live_credentials": state.settings.missing_live_credentials(),
        "persistence": "POSTGRESQL" if state.repository else "IN_MEMORY",
    }


@router.get("/fleet")
def fleet(request: Request) -> dict[str, Any]:
    state = app_state(request)
    return {"depot": state.fleet[0].start, "vehicles": state.fleet, "orders": state.orders}


@router.post("/plans/generate")
async def generate_plan(body: GeneratePlanRequest, request: Request):
    state = app_state(request)
    if body.parent_plan_id and body.parent_plan_id in state.plans:
        plan_id = body.parent_plan_id
        version = state.plans[plan_id][-1].version + 1
    else:
        plan_id = None
        version = 1
    speed_context: dict[str, float] | None = None
    source_data_version = body.source_data_version
    if state.repository:
        speed_context, speed_version = await state.repository.nearest_speed_context(
            tuple(
                (order.order_id, order.location.lat, order.location.lon)
                for order in state.orders
            )
        )
        if speed_version:
            source_data_version = f"{source_data_version}+lta-v4:{speed_version}"
    plan = build_plan(
        state.fleet,
        state.orders,
        plan_id=plan_id,
        version=version,
        source_data_version=source_data_version,
        speed_kph_by_stop=speed_context,
    )
    if state.settings.onemap_access_token:
        plan = await enrich_plan_geometry(plan, state.fleet[0].start, state.onemap)
    state.plans[plan.plan_id].append(plan)
    await state.save_plan(plan)
    await state.record_audit(
        "PLAN_GENERATED", "route-planning-agent", {"plan_id": plan.plan_id, "version": plan.version}
    )
    return plan


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
    plan = get_plan(plan_id, version, request)
    violations = validate_plan(plan, app_state(request).fleet, app_state(request).orders)
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
    return activated


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
def update_scenario(scenario_id: str, body: ClockUpdate, request: Request):
    try:
        return app_state(request).simulation.update(scenario_id, **body.model_dump())
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


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
    if state.repository:
        await state.repository.save_disruption(event)
    await state.record_audit("DISRUPTION_INJECTED", "dispatcher", body.model_dump(mode="json"))
    return event


@router.get("/map/state")
def map_state(request: Request, scenario_id: str = "demo") -> dict[str, Any]:
    state = app_state(request)
    clock = state.simulation.get(scenario_id)
    plan = state.latest_plan
    trucks = []
    for route in plan.routes:
        position = state.fleet[0].start
        phase = "AT_DEPOT"
        for stop in route.stops:
            if clock.current_minute >= stop.eta_minute:
                position = stop.location
                phase = "SERVICING" if clock.current_minute < stop.departure_minute else "EN_ROUTE"
                continue
            previous_departure = 480
            previous_position = state.fleet[0].start
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
    if state.repository:
        await state.repository.save_conversation_message(
            ConversationMessage(
                conversation_id=body.conversation_id,
                role="USER",
                content=body.message,
                trust_label="HUMAN_DISPATCHER",
                expires_at=datetime.now(UTC)
                + timedelta(days=state.settings.conversation_retention_days),
            )
        )
    lowered = body.message.casefold()
    if any(word in lowered for word in ("closure", "breakdown", "rain", "urgent")):
        task_type = "DISRUPTION_ANALYSIS"
    elif any(word in lowered for word in ("route", "plan", "optimize", "replan")):
        task_type = "ROUTE_PLAN"
    else:
        task_type = "DRIVER_ENQUIRY"
    task = AgentTask(
        task_type=task_type,
        requester="dispatcher",
        conversation_id=body.conversation_id,
        input_references=(f"message:{hashlib.sha256(body.message.encode()).hexdigest()[:12]}",),
        trust_labels=("HUMAN_DISPATCHER",),
    )
    result = state.agents.invoke(task)
    await state.record_audit(
        "AGENT_TASK_COMPLETED",
        "master-dispatcher-agent",
        {"task_id": task.task_id, "type": task_type},
    )
    fallback = _explain_result(task_type, result.computed_metrics)
    reply = await asyncio.to_thread(state.openai.explain, task, result, fallback)
    if state.repository:
        await state.repository.save_conversation_message(
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
    }


def _explain_result(task_type: str, metrics: dict[str, Any]) -> str:
    if task_type == "ROUTE_PLAN":
        violations = metrics.get("hard_violations", 0)
        return (
            "I prepared a candidate-planning action. The validation trace reports "
            f"{violations} hard violations; activation remains policy-controlled."
        )
    if task_type == "DISRUPTION_ANALYSIS":
        return (
            "I bounded the disruption impact and proposed a replan. "
            "No plan was edited or activated."
        )
    return "I drafted a driver response. It has not been sent."


@router.post("/approvals")
async def create_approval(body: ApprovalCreate, request: Request):
    state = app_state(request)
    get_plan(body.plan_id, body.plan_version, request)
    approval = state.approvals.create(body.plan_id, body.plan_version, body.action)
    if state.repository:
        await state.repository.save_approval(approval)
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
    if state.repository:
        await state.repository.save_approval(result, str(body.proof.get("nonce", "")))
    await state.record_audit(
        "APPROVAL_EXECUTED", "x401-verifier", {"approval_id": approval_id}
    )
    return result


@router.post("/telegram/enrollment-tokens")
def issue_enrollment(body: EnrollmentIssue, request: Request):
    return {"token": app_state(request).enrollment.issue(body.driver_id), "expires_in_seconds": 600}


@router.post("/telegram/drivers/{driver_id}/suspend")
async def suspend_driver(driver_id: str, request: Request):
    app_state(request).enrollment.suspend(driver_id)
    await app_state(request).record_audit(
        "DRIVER_SUSPENDED", "dispatcher", {"driver_id": driver_id}
    )
    return {"driver_id": driver_id, "status": "SUSPENDED"}


@router.post("/telegram/webhook")
def telegram_webhook(
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
            driver = state.enrollment.enroll(token, int(sender["id"]), str(chat["type"]))
        except (KeyError, PermissionError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
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
        if state.repository:
            await state.repository.save_memory(item)
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
        if state.repository:
            embedding = await asyncio.to_thread(state.openai.embed, item.content)
            await state.repository.save_memory(item, embedding)
        return item
    except KeyError as exc:
        raise HTTPException(404, "memory not found") from exc


@router.post("/memory/{memory_id}/supersede")
async def supersede_memory(memory_id: str, body: MemoryReplacement, request: Request):
    try:
        state = app_state(request)
        item = state.memory.supersede(memory_id, body.content)
        if state.repository:
            await state.repository.save_memory(state.memory.get(memory_id))
            embedding = await asyncio.to_thread(state.openai.embed, item.content)
            await state.repository.save_memory(item, embedding)
        return item
    except KeyError as exc:
        raise HTTPException(404, "memory not found") from exc


@router.get("/memory/search")
async def search_memory(request: Request, q: str):
    state = app_state(request)
    if not state.repository:
        return state.memory.search(q)
    embedding = await asyncio.to_thread(state.openai.embed, q)
    return await state.repository.search_curated_memory(q, embedding)


@router.get("/memory/conversations/search")
async def search_conversations(request: Request, q: str):
    state = app_state(request)
    if not state.repository:
        return []
    return await state.repository.search_conversations(q)


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
