"""LLM-callable tool wrappers around MahJourney's deterministic domain functions.

Phase 2 of the agentic re-engineering. The worker agents no longer decide *in
Python* which domain function to run; instead an LLM chooses tools by name and
the runner here executes the real, deterministic computation and returns a
JSON-serialisable result the model can reason over.

Design notes:

* **No dependency on ``agents.py``.** Tools operate on a small, explicit
  :class:`ToolContext` (plan, fleet, orders, disruptions, driver id) built by
  the caller from its read-only ``AgentContext``. This avoids a circular import
  and keeps the tool layer independently testable.
* **Read-only / propose-only.** Every tool computes and returns numbers; none
  mutate state, activate a plan, or send anything. The worst a tool can do is
  read the current plan. This preserves the propose-only invariant even once the
  LLM is in the driver's seat.
* **Each tool is a plain function** taking ``(context, **args)`` and returning a
  ``dict``. Tools ignore arguments they don't need, so a model that passes an
  extra field cannot break them. Unknown tool names raise :class:`ToolError`,
  which the worker loop surfaces back to the model rather than crashing the turn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .disruptions import disruption_speed_kph_by_stop
from .domain import DisruptionEvent, Order, PlanVersion, Vehicle
from .planning import reapply_route_timing, validate_plan


class ToolError(Exception):
    """Raised when a tool is unknown or its inputs are unusable.

    The worker loop catches this and feeds the message back to the model as a
    tool result, so a bad tool call becomes a recoverable turn event rather than
    an unhandled exception.
    """


@dataclass(frozen=True)
class ToolContext:
    """The read-only slice of the world a worker's tools may see.

    Built by the worker from its ``AgentContext``. Optional everywhere so tools
    degrade gracefully (returning an ``unavailable`` result) when a snapshot is
    missing — for example in a unit test with no plan.
    """

    plan: PlanVersion | None = None
    fleet: tuple[Vehicle, ...] = ()
    orders: tuple[Order, ...] = ()
    max_stops_per_vehicle: int = 25
    enforce_delivery_windows: bool = True
    active_disruptions: tuple[DisruptionEvent, ...] = ()
    driver_id: str | None = None
    # Pre-fetched external context, summarised by the API layer before the graph
    # runs (the agent graph is synchronous, so all network I/O happens at the
    # async boundary and the tools here only read these snapshots). Each is a
    # small JSON-serialisable dict or None when unavailable / not configured.
    traffic_conditions: dict[str, Any] | None = None
    weather_conditions: dict[str, Any] | None = None


def _minute_to_clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


# --- Tool implementations ----------------------------------------------------
# Each takes (context, **kwargs) and returns a JSON-serialisable dict. They wrap
# the existing domain functions unchanged.


def validate_current_plan(context: ToolContext, **_: Any) -> dict[str, Any]:
    """Re-validate the current plan against every hard constraint.

    Returns the list of hard-constraint violations and a coverage summary
    (assigned stops, active routes, unassigned orders). This is the route
    planner's primary evidence tool.
    """
    if context.plan is None:
        return {"available": False, "reason": "no active plan snapshot"}
    violations = validate_plan(
        context.plan,
        context.fleet,
        context.orders,
        context.max_stops_per_vehicle,
        enforce_delivery_windows=context.enforce_delivery_windows,
    )
    assigned = sum(len(route.stops) for route in context.plan.routes)
    unassigned = sum(1 for v in violations if v.startswith("unassigned"))
    active_routes = sum(1 for route in context.plan.routes if route.stops)
    return {
        "available": True,
        "hard_violations": list(violations),
        "hard_violation_count": len(violations),
        "unassigned_orders": unassigned,
        "assigned_stops": assigned,
        "active_routes": active_routes,
        "vehicles": len(context.plan.routes),
        "objective_cost_km": context.plan.objective_cost,
        "plan_status": context.plan.status,
    }


def assess_disruption_impact(context: ToolContext, **_: Any) -> dict[str, Any]:
    """Recompute the current plan's timing under the active disruptions.

    Keeps the existing stop assignment and sequence (this is not a replan) and
    reports the added minutes across the fleet and the worst-affected vehicle —
    a real deterministic before/after delta, not an estimate.
    """
    if context.plan is None or not context.plan.routes:
        return {"available": False, "reason": "no active plan with routes"}
    if not context.active_disruptions:
        return {
            "available": True,
            "active_disruptions": 0,
            "added_minutes_total": 0,
            "note": "no active disruptions; current plan timing is unchanged",
        }
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
    before_duration = sum(route.duration_minutes for route in context.plan.routes)
    after_duration = sum(route.duration_minutes for route in stressed.routes)
    added_minutes = after_duration - before_duration
    worst_route = max(
        stressed.routes, key=lambda route: route.duration_minutes, default=None
    )
    return {
        "available": True,
        "active_disruptions": len(context.active_disruptions),
        "disruption_types": sorted(
            {str(e.event_type) for e in context.active_disruptions}
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


def lookup_driver_assignment(
    context: ToolContext, driver_id: str | None = None, **_: Any
) -> dict[str, Any]:
    """Look up one driver's own assignment: vehicle, stop count, and ETAs.

    Scoped strictly to the enquiring driver — it never returns anything about
    other drivers, preserving the driver-scoped boundary. ``driver_id`` defaults
    to the context's bound driver when the model omits it.
    """
    target = driver_id or context.driver_id
    if not target:
        return {"available": False, "reason": "no driver id supplied"}
    if context.plan is None:
        return {"available": False, "reason": "no active plan snapshot", "driver_id": target}
    route = next(
        (r for r in context.plan.routes if r.driver_id == target), None
    )
    if route is None or not route.stops:
        return {"available": True, "driver_id": target, "assigned_stops": 0}
    first, last = route.stops[0], route.stops[-1]
    return {
        "available": True,
        "driver_id": target,
        "vehicle_id": route.vehicle_id,
        "assigned_stops": len(route.stops),
        "first_eta": _minute_to_clock(first.eta_minute),
        "last_eta": _minute_to_clock(last.eta_minute),
        "route_distance_km": route.distance_km,
    }


def report_vehicle_breakdown(
    context: ToolContext,
    description: str = "",
    location: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Record a driver-reported vehicle breakdown and return confirmation data.

    Stores the structured report on the context so the agent can confirm it
    back to the driver and surface it for the dispatcher. The tool does NOT
    automatically reassign stops or alter the active plan — that requires a
    dispatcher decision. ``description`` is a free-text note from the driver
    (e.g. "flat tyre", "engine overheated"); ``location`` is optional
    (a landmark, address, or "last stop").
    """
    driver_id = context.driver_id
    if not driver_id:
        return {"recorded": False, "reason": "no driver identity available"}
    route = next(
        (r for r in (context.plan.routes if context.plan else []) if r.driver_id == driver_id),
        None,
    )
    remaining_stops = 0
    vehicle_id = None
    if route:
        vehicle_id = route.vehicle_id
        # Stops are already ordered by sequence; all are "remaining" since
        # we don't have the actual current stop index here — the dispatcher
        # sees the full count and can triage accordingly.
        remaining_stops = len(route.stops)
    # Prefer the human-readable license plate for the driver-facing confirmation
    # ("...for vehicle SGX1234A"). Fall back to the internal vehicle_id only when
    # the plate is unknown so the confirmation always names something concrete.
    vehicle = next(
        (v for v in context.fleet if v.vehicle_id == vehicle_id), None
    )
    license_plate = vehicle.license_plate if vehicle and vehicle.license_plate else None
    vehicle_label = license_plate or vehicle_id
    return {
        "recorded": True,
        "driver_id": driver_id,
        "vehicle_id": vehicle_id,
        "license_plate": license_plate,
        # The identifier to name in the driver reply — plate when known, else id.
        "vehicle_label": vehicle_label,
        "description": description or "No description provided",
        # ``location`` is optional. When the driver didn't give one we return
        # None (not a "Not specified" sentinel) so the driver-facing reply never
        # tells the driver their location was missing — a missing location is
        # normal and the dispatcher can follow up. ``remaining_stops`` is kept
        # for the dispatcher's audit/notification only; the driver reply must
        # not surface a stop count.
        "location": location or None,
        "remaining_stops": remaining_stops,
        "dispatcher_action_required": True,
        # Guidance for the reply layer: confirm formally, don't quote raw fields.
        "driver_message_guidance": (
            "Confirm the breakdown has been logged for the named vehicle, state "
            "that the dispatcher has been informed, and ask the driver to wait "
            "for further instructions. Keep the tone professional. Do not "
            "mention stop counts or a missing location."
        ),
    }


def get_driver_route_summary(
    context: ToolContext, **_: Any
) -> dict[str, Any]:
    """Return a driver-readable summary of their own current route.

    Includes vehicle, total stops, first/last ETA, and the next upcoming stop
    (stop 1 if no progress information is available). Scoped to the enquiring
    driver only — never returns another driver's data.
    """
    driver_id = context.driver_id
    if not driver_id:
        return {"available": False, "reason": "no driver identity available"}
    if context.plan is None:
        return {"available": False, "reason": "no active plan", "driver_id": driver_id}
    route = next(
        (r for r in context.plan.routes if r.driver_id == driver_id), None
    )
    if route is None or not route.stops:
        return {"available": True, "driver_id": driver_id, "assigned_stops": 0,
                "message": "You have no stops assigned in the current plan."}
    first, last = route.stops[0], route.stops[-1]
    next_stop = route.stops[0]
    stops_detail = [
        {
            "sequence": s.sequence,
            "eta": _minute_to_clock(s.eta_minute),
            "stop_id": s.stop_id,
        }
        for s in route.stops
    ]
    return {
        "available": True,
        "driver_id": driver_id,
        "vehicle_id": route.vehicle_id,
        "assigned_stops": len(route.stops),
        "distance_km": route.distance_km,
        "duration_minutes": route.duration_minutes,
        "first_eta": _minute_to_clock(first.eta_minute),
        "last_eta": _minute_to_clock(last.eta_minute),
        "next_stop_sequence": next_stop.sequence,
        "next_stop_eta": _minute_to_clock(next_stop.eta_minute),
        "stops": stops_detail,
    }


def request_route_dispatch(
    context: ToolContext, **_: Any
) -> dict[str, Any]:
    """Signal that the driver is requesting their full route schedule be sent.

    This tool does not perform the send itself — it verifies the driver has
    an assigned route and returns a ``send_route: True`` flag that the caller
    (``handle_driver_message``) acts on by calling ``format_route_message`` and
    sending it via Telegram. This preserves the propose-only tool boundary.
    """
    driver_id = context.driver_id
    if not driver_id:
        return {"send_route": False, "reason": "no driver identity available"}
    if context.plan is None:
        return {"send_route": False, "reason": "no active plan", "driver_id": driver_id}
    route = next(
        (r for r in context.plan.routes if r.driver_id == driver_id), None
    )
    if route is None or not route.stops:
        return {
            "send_route": False,
            "driver_id": driver_id,
            "reason": "no stops assigned in the current plan",
        }
    first, last = route.stops[0], route.stops[-1]
    return {
        "send_route": True,
        "driver_id": driver_id,
        "vehicle_id": route.vehicle_id,
        "assigned_stops": len(route.stops),
        "first_eta": _minute_to_clock(first.eta_minute),
        "last_eta": _minute_to_clock(last.eta_minute),
        "distance_km": route.distance_km,
    }


def get_traffic_conditions(context: ToolContext, **_: Any) -> dict[str, Any]:
    """Return current Singapore road-traffic conditions from LTA DataMall.

    Reads the traffic snapshot the API layer fetched before this turn (incidents
    and speed bands, summarised). Returns ``{"available": false, ...}`` when LTA
    is not configured or the fetch failed, so the model can reason about the gap
    rather than receiving fabricated data.
    """
    if not context.traffic_conditions:
        return {"available": False, "reason": "no live traffic snapshot available"}
    return {"available": True, **context.traffic_conditions}


def get_weather_conditions(context: ToolContext, **_: Any) -> dict[str, Any]:
    """Return current Singapore weather from NEA (rainfall + 2-hour forecast).

    Reads the weather snapshot the API layer fetched before this turn. Returns
    ``{"available": false, ...}`` when NEA is unavailable, matching the graceful
    degradation of the other external tools.
    """
    if not context.weather_conditions:
        return {"available": False, "reason": "no live weather snapshot available"}
    return {"available": True, **context.weather_conditions}


# --- Tool specifications -----------------------------------------------------
# A ToolSpec bundles the callable with the metadata the model needs to call it:
# a name, a human-readable description, and a JSON-schema for its parameters
# (OpenAI tool-calling format). Tools with no parameters expose an empty object.


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]

    def openai_tool(self) -> dict[str, Any]:
        """Render this spec as an OpenAI Responses-API function tool entry."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

VALIDATE_CURRENT_PLAN = ToolSpec(
    name="validate_current_plan",
    description=(
        "Re-validate the current delivery plan against all hard constraints "
        "(capacity, time windows, working hours, max stops) and summarise "
        "coverage. Use to check plan feasibility before proposing a replan."
    ),
    parameters=_NO_PARAMS,
    handler=validate_current_plan,
)

ASSESS_DISRUPTION_IMPACT = ToolSpec(
    name="assess_disruption_impact",
    description=(
        "Recompute how much slower the current plan becomes under the active "
        "disruptions (road closures, heavy rain, breakdowns), keeping the same "
        "stop assignment. Returns added minutes and the worst-affected vehicle."
    ),
    parameters=_NO_PARAMS,
    handler=assess_disruption_impact,
)

LOOKUP_DRIVER_ASSIGNMENT = ToolSpec(
    name="lookup_driver_assignment",
    description=(
        "Look up a single driver's own assignment: their vehicle, number of "
        "stops, and first/last ETA. Scoped to the enquiring driver only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "driver_id": {
                "type": "string",
                "description": (
                    "The driver to look up. Omit to use the enquiring driver "
                    "already bound to this request."
                ),
            }
        },
        "additionalProperties": False,
    },
    handler=lookup_driver_assignment,
)


@dataclass(frozen=True)
class ToolRegistry:
    """An immutable set of tools a worker may call, indexed by name."""

    specs: tuple[ToolSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        by_name = {spec.name: spec for spec in self.specs}
        object.__setattr__(self, "_by_name", by_name)

    def openai_tools(self) -> list[dict[str, Any]]:
        """The tool list to pass to the OpenAI Responses API."""
        return [spec.openai_tool() for spec in self.specs]

    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def run(
        self, name: str, context: ToolContext, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Dispatch a tool call by name and return its JSON-serialisable result.

        Raises :class:`ToolError` for an unknown tool name so the caller can
        feed the error back to the model instead of crashing the turn.
        """
        spec = self._by_name.get(name)  # type: ignore[attr-defined]
        if spec is None:
            raise ToolError(f"unknown tool: {name!r}")
        return spec.handler(context, **(arguments or {}))


GET_TRAFFIC_CONDITIONS = ToolSpec(
    name="get_traffic_conditions",
    description=(
        "Get current Singapore road-traffic conditions (incidents and speed "
        "bands) from LTA DataMall. Use to ground a disruption assessment in "
        "live traffic. May report that no live data is available."
    ),
    parameters=_NO_PARAMS,
    handler=get_traffic_conditions,
)

GET_WEATHER_CONDITIONS = ToolSpec(
    name="get_weather_conditions",
    description=(
        "Get current Singapore weather (rainfall readings and the 2-hour "
        "forecast) from NEA. Use to check whether rain is affecting the fleet. "
        "May report that no live data is available."
    ),
    parameters=_NO_PARAMS,
    handler=get_weather_conditions,
)

REPORT_VEHICLE_BREAKDOWN = ToolSpec(
    name="report_vehicle_breakdown",
    description=(
        "Record a driver's vehicle breakdown report. Call this when the driver "
        "says their truck/vehicle has broken down, won't start, has a flat tyre, "
        "or cannot continue the route for a mechanical reason. "
        "Returns a confirmation with remaining stop count so the agent can "
        "relay it back to the driver and flag it for the dispatcher."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "Brief description of the breakdown (e.g. 'flat tyre', "
                    "'engine overheated', 'won't start'). Use the driver's "
                    "own words where possible."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "Where the breakdown occurred — a landmark, road name, "
                    "postal code, or 'near stop N'. Leave blank if unknown."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=report_vehicle_breakdown,
)

GET_DRIVER_ROUTE_SUMMARY = ToolSpec(
    name="get_driver_route_summary",
    description=(
        "Retrieve the current driver's own route: vehicle id, total stops, "
        "first and last ETAs, and the next upcoming stop ETA. Use when the "
        "driver asks about their schedule, next stop, or how many deliveries "
        "remain. Never returns another driver's data."
    ),
    parameters=_NO_PARAMS,
    handler=get_driver_route_summary,
)


# Per-worker tool registries. Each worker only sees the tools appropriate to its
# role, mirroring the (currently disconnected) skill-contract allowlists. The
# disruption worker also gets the external LTA/NEA tools so it can ground its
# analysis in live traffic and weather.
ROUTE_PLANNING_TOOLS = ToolRegistry((VALIDATE_CURRENT_PLAN,))
DISRUPTION_TOOLS = ToolRegistry(
    (
        ASSESS_DISRUPTION_IMPACT,
        VALIDATE_CURRENT_PLAN,
        GET_TRAFFIC_CONDITIONS,
        GET_WEATHER_CONDITIONS,
    )
)
DRIVER_COMMS_TOOLS = ToolRegistry((LOOKUP_DRIVER_ASSIGNMENT,))
REQUEST_ROUTE_DISPATCH = ToolSpec(
    name="request_route_dispatch",
    description=(
        "Use when the driver asks to receive their full route schedule or delivery "
        "plan (e.g. 'send me my route', 'what are my stops', 'resend my schedule'). "
        "Verifies the driver has an active route and signals the system to send the "
        "full stop-by-stop schedule with navigation links to their Telegram chat."
    ),
    parameters=_NO_PARAMS,
    handler=request_route_dispatch,
)

DRIVER_AGENT_TOOLS = ToolRegistry(
    (
        REQUEST_ROUTE_DISPATCH,
        GET_DRIVER_ROUTE_SUMMARY,
        REPORT_VEHICLE_BREAKDOWN,
        LOOKUP_DRIVER_ASSIGNMENT,
    )
)
