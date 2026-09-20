from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .disruptions import disruption_speed_kph_by_stop
from .domain import (
    AgentResult,
    AgentTask,
    DisruptionEvent,
    FrozenModel,
    Order,
    PlanVersion,
    Vehicle,
)
from .memory import MasterMemory
from .planning import reapply_route_timing, validate_plan


class AgentContext(FrozenModel):
    """Read-only snapshot handed to worker nodes.

    Workers receive this instead of any mutable service or the master-memory
    object, so they can compute real metrics from the current plan and fleet
    without the ability to change state or read curated memory. All fields are
    optional so the graph still runs (with safe defaults) when no snapshot is
    supplied, for example in unit tests.
    """

    plan: PlanVersion | None = None
    fleet: tuple[Vehicle, ...] = ()
    orders: tuple[Order, ...] = ()
    max_stops_per_vehicle: int = 25
    enforce_delivery_windows: bool = True
    active_disruptions: tuple[DisruptionEvent, ...] = ()
    driver_id: str | None = None


class GraphState(TypedDict, total=False):
    task: AgentTask
    context: AgentContext
    result: AgentResult
    delegated_to: str


# Single source of truth for turning a dispatcher message into a task type.
# Checked in order; the first matching group wins. The master agent owns this
# classification — callers should not re-derive it.
#
# Every real task type requires a POSITIVE keyword match. A message matching
# none of them is genuinely ambiguous and becomes CLARIFICATION_NEEDED rather
# than being guessed as a driver enquiry — defaulting an unrecognized request
# to a specific worker would be a silent misclassification, not a safe choice.
_TASK_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DISRUPTION_ANALYSIS", ("closure", "breakdown", "rain", "urgent", "disruption", "flood")),
    ("ROUTE_PLAN", ("route", "plan", "replan", "optimize", "optimise", "reassign")),
    (
        "DRIVER_ENQUIRY",
        ("driver", "eta", "where is", "assignment", "assigned", "stop", "delivery"),
    ),
)
CLARIFICATION_NEEDED = "CLARIFICATION_NEEDED"

# Explicit task-type -> worker routing. CLARIFICATION_NEEDED and any other
# unrecognized type route to the master's own clarification node, never to a
# worker, since guessing which worker should handle an unclassifiable request
# is not a safe default.
_TASK_TYPE_TO_WORKER: dict[str, str] = {
    "ROUTE_PLAN": "route_planning",
    "DISRUPTION_ANALYSIS": "disruption",
    "DRIVER_ENQUIRY": "driver_comms",
}


def classify_task_type(message: str) -> str:
    """Classify a raw dispatcher message into a canonical task type.

    This is the one place a message is interpreted into a task type; the API and
    the master node both defer to it so the two can never drift apart. Returns
    :data:`CLARIFICATION_NEEDED` when no task type's keywords match.
    """
    lowered = message.casefold()
    for task_type, keywords in _TASK_TYPE_KEYWORDS:
        if any(word in lowered for word in keywords):
            return task_type
    return CLARIFICATION_NEEDED


def _minute_to_clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


class AgentSystem:
    """Bounded dispatcher graph.

    Worker nodes never receive the master-memory object; they operate only on
    the task and a read-only :class:`AgentContext`. Workers compute real metrics
    but only ever *propose* actions — activation and execution remain with the
    deterministic policy and approval layers.
    """

    def __init__(self, memory: MasterMemory) -> None:
        self._master_memory = memory
        graph = StateGraph(GraphState)
        graph.add_node("master", self._master)
        graph.add_node("route_planning", self._route_planning)
        graph.add_node("disruption", self._disruption)
        graph.add_node("driver_comms", self._driver_comms)
        graph.add_node("clarify", self._clarify)
        graph.set_entry_point("master")
        graph.add_conditional_edges(
            "master",
            lambda state: state["delegated_to"],
            {
                "route_planning": "route_planning",
                "disruption": "disruption",
                "driver_comms": "driver_comms",
                "clarify": "clarify",
            },
        )
        graph.add_edge("route_planning", END)
        graph.add_edge("disruption", END)
        graph.add_edge("driver_comms", END)
        graph.add_edge("clarify", END)
        self.graph = graph.compile()

    def _master(self, state: GraphState) -> GraphState:
        # The master owns delegation. It routes on the task type via an explicit
        # mapping; a type with no defined worker (including CLARIFICATION_NEEDED)
        # is handled by the master itself rather than being guessed onto a
        # worker that has no basis to answer it.
        task = state["task"]
        delegated_to = _TASK_TYPE_TO_WORKER.get(task.task_type.upper(), "clarify")
        return {"delegated_to": delegated_to}

    @staticmethod
    def _clarify(state: GraphState) -> GraphState:
        # The request did not match any known task type. Rather than guessing a
        # worker, the master asks the dispatcher to clarify what they need.
        task = state["task"]
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="NEEDS_INPUT",
                evidence_references=task.input_references,
                computed_metrics={"tool_calls": 0},
                escalation_reason=(
                    "I could not tell whether this is about a route/plan, a disruption, "
                    "or a driver's assignment."
                ),
            )
        }

    @staticmethod
    def _route_planning(state: GraphState) -> GraphState:
        task = state["task"]
        context = state.get("context") or AgentContext()
        metrics: dict[str, Any] = {"tool_calls": 0}
        warnings: list[str] = []
        if context.plan is not None:
            # Tool 1: re-validate the current plan against every hard constraint.
            violations = validate_plan(
                context.plan,
                context.fleet,
                context.orders,
                context.max_stops_per_vehicle,
                enforce_delivery_windows=context.enforce_delivery_windows,
            )
            # Tool 2: summarize assignment coverage from the plan itself.
            assigned = sum(len(route.stops) for route in context.plan.routes)
            unassigned = sum(1 for v in violations if v.startswith("unassigned"))
            active_routes = sum(1 for route in context.plan.routes if route.stops)
            metrics = {
                "tool_calls": 2,
                "hard_violations": len(violations),
                "unassigned_orders": unassigned,
                "assigned_stops": assigned,
                "active_routes": active_routes,
                "vehicles": len(context.plan.routes),
                "objective_cost_km": context.plan.objective_cost,
                "plan_status": context.plan.status,
            }
            if unassigned:
                warnings.append(f"{unassigned} order(s) could not be assigned within constraints")
        else:
            # No plan snapshot (e.g. unit test): stay bounded and non-committal.
            metrics["hard_violations"] = 0
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics=metrics,
                proposed_actions=({"type": "GENERATE_CANDIDATE_PLAN"},),
                warnings=tuple(warnings),
            )
        }

    @staticmethod
    def _disruption(state: GraphState) -> GraphState:
        task = state["task"]
        context = state.get("context") or AgentContext()
        metrics: dict[str, Any] = {"tool_calls": 0}
        warnings: list[str] = []
        if context.plan is not None and context.plan.routes and context.active_disruptions:
            # Tool 1: recompute the plan's timing under the active road-closure
            # and/or heavy-rain disruptions, keeping the same stop assignment.
            # This is a real, deterministic before/after delta, not an estimate.
            speed_context = disruption_speed_kph_by_stop(
                context.orders, context.active_disruptions, None
            )
            stressed = reapply_route_timing(
                context.plan,
                context.fleet,
                context.orders,
                speed_context,
                enforce_delivery_windows=context.enforce_delivery_windows,
            )
            # Tool 2: compare against the plan as it stands today.
            before_duration = sum(route.duration_minutes for route in context.plan.routes)
            after_duration = sum(route.duration_minutes for route in stressed.routes)
            added_minutes = after_duration - before_duration
            worst_route = max(
                stressed.routes, key=lambda route: route.duration_minutes, default=None
            )
            metrics = {
                "tool_calls": 2,
                "disruption_types": ",".join(
                    sorted({str(e.event_type) for e in context.active_disruptions})
                ),
                "added_minutes_total": added_minutes,
                "objective_cost_delta_km": round(
                    stressed.objective_cost - context.plan.objective_cost, 2
                ),
                "worst_affected_vehicle": worst_route.vehicle_id if worst_route else None,
                "worst_route_duration_minutes": (
                    worst_route.duration_minutes if worst_route else 0
                ),
            }
            if added_minutes > 0:
                warnings.append(
                    f"active disruption adds {added_minutes} minute(s) across the fleet"
                )
        elif context.plan is not None and context.plan.routes:
            metrics["tool_calls"] = 1
            metrics["added_minutes_total"] = 0
        else:
            metrics["tool_calls"] = 1
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics=metrics,
                proposed_actions=({"type": "BOUNDED_REPLAN", "requires_policy_check": True},),
                warnings=tuple(warnings),
            )
        }

    @staticmethod
    def _driver_comms(state: GraphState) -> GraphState:
        task = state["task"]
        context = state.get("context") or AgentContext()
        metrics: dict[str, Any] = {"tool_calls": 1}
        route = None
        if context.plan is not None and context.driver_id:
            # Tool 1: look up the enquiring driver's own assignment (and nothing
            # about other drivers, preserving the driver-scoped boundary).
            route = next(
                (r for r in context.plan.routes if r.driver_id == context.driver_id), None
            )
        if route is not None and route.stops:
            first, last = route.stops[0], route.stops[-1]
            metrics = {
                "tool_calls": 1,
                "driver_id": context.driver_id,
                "vehicle_id": route.vehicle_id,
                "assigned_stops": len(route.stops),
                "first_eta": _minute_to_clock(first.eta_minute),
                "last_eta": _minute_to_clock(last.eta_minute),
                "route_distance_km": route.distance_km,
            }
        elif context.driver_id:
            metrics = {"tool_calls": 1, "driver_id": context.driver_id, "assigned_stops": 0}
        return {
            "result": AgentResult(
                task_id=task.task_id,
                status="COMPLETED",
                evidence_references=task.input_references,
                computed_metrics=metrics,
                proposed_actions=({"type": "DRAFT_DRIVER_REPLY", "send": False},),
            )
        }

    def invoke(self, task: AgentTask, context: AgentContext | None = None) -> AgentResult:
        state: GraphState = {"task": task}
        if context is not None:
            state["context"] = context
        final: dict[str, Any] = self.graph.invoke(state)
        return final["result"]
