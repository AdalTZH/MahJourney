"""Unit tests for the Phase 2 LLM tool wrappers.

These verify each tool wraps its domain function correctly and degrades
gracefully with no snapshot, independent of the agent graph.
"""

from mahjourney.domain import DisruptionEvent
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.planning import build_plan, validate_plan
from mahjourney.tools import (
    DISRUPTION_TOOLS,
    DRIVER_COMMS_TOOLS,
    ROUTE_PLANNING_TOOLS,
    ToolContext,
    ToolError,
    ToolRegistry,
    assess_disruption_impact,
    get_traffic_conditions,
    get_weather_conditions,
    lookup_driver_assignment,
    validate_current_plan,
)


def _context_with_plan(**overrides) -> ToolContext:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    base = {"plan": plan, "fleet": fleet, "orders": orders}
    base.update(overrides)
    return ToolContext(**base)


def test_validate_current_plan_matches_domain_validation() -> None:
    ctx = _context_with_plan()
    out = validate_current_plan(ctx)
    expected = validate_plan(ctx.plan, ctx.fleet, ctx.orders, ctx.max_stops_per_vehicle)
    assert out["available"] is True
    assert out["hard_violation_count"] == len(expected)
    assert out["assigned_stops"] == sum(len(r.stops) for r in ctx.plan.routes)
    assert out["vehicles"] == len(ctx.plan.routes)


def test_validate_current_plan_unavailable_without_plan() -> None:
    out = validate_current_plan(ToolContext())
    assert out["available"] is False


def test_assess_disruption_impact_no_disruptions_is_zero() -> None:
    ctx = _context_with_plan()
    out = assess_disruption_impact(ctx)
    assert out["available"] is True
    assert out["added_minutes_total"] == 0


def test_assess_disruption_impact_reports_added_minutes() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    disruption = DisruptionEvent(
        scenario_id="demo",
        event_type="HEAVY_RAIN",
        effective_minute=480,
        payload={},
    )
    ctx = ToolContext(
        plan=plan, fleet=fleet, orders=orders, active_disruptions=(disruption,)
    )
    out = assess_disruption_impact(ctx)
    assert out["available"] is True
    assert out["active_disruptions"] == 1
    assert "HEAVY_RAIN" in out["disruption_types"]
    # Heavy rain slows travel, so timing should not improve.
    assert out["added_minutes_total"] >= 0


def test_lookup_driver_assignment_scopes_to_one_driver() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    driver = fleet[0].driver_id
    ctx = ToolContext(plan=plan, fleet=fleet, orders=orders, driver_id=driver)
    out = lookup_driver_assignment(ctx)
    assert out["available"] is True
    assert out["driver_id"] == driver
    # Explicit driver_id arg overrides the bound one.
    out2 = lookup_driver_assignment(ctx, driver_id=fleet[1].driver_id)
    assert out2["driver_id"] == fleet[1].driver_id


def test_lookup_driver_assignment_needs_a_driver() -> None:
    out = lookup_driver_assignment(ToolContext())
    assert out["available"] is False


def test_registry_runs_tools_by_name_and_rejects_unknown() -> None:
    ctx = _context_with_plan()
    out = ROUTE_PLANNING_TOOLS.run("validate_current_plan", ctx)
    assert out["available"] is True
    try:
        ROUTE_PLANNING_TOOLS.run("does_not_exist", ctx)
    except ToolError:
        pass
    else:
        raise AssertionError("expected ToolError for unknown tool")


def test_external_tools_report_unavailable_without_snapshot() -> None:
    ctx = ToolContext()
    assert get_traffic_conditions(ctx)["available"] is False
    assert get_weather_conditions(ctx)["available"] is False


def test_external_tools_pass_through_prefetched_snapshot() -> None:
    ctx = ToolContext(
        traffic_conditions={"source": "LTA DataMall", "incident_count": 3},
        weather_conditions={"source": "NEA", "rain_detected": True},
    )
    traffic = get_traffic_conditions(ctx)
    weather = get_weather_conditions(ctx)
    assert traffic["available"] is True and traffic["incident_count"] == 3
    assert weather["available"] is True and weather["rain_detected"] is True


def test_openai_tool_schema_shape() -> None:
    tools = DISRUPTION_TOOLS.openai_tools()
    assert {t["name"] for t in tools} == {
        "assess_disruption_impact",
        "validate_current_plan",
        "get_traffic_conditions",
        "get_weather_conditions",
    }
    for tool in tools:
        assert tool["type"] == "function"
        assert tool["parameters"]["type"] == "object"


def test_per_worker_registries_expose_expected_tools() -> None:
    assert ROUTE_PLANNING_TOOLS.names() == ("validate_current_plan",)
    assert set(DISRUPTION_TOOLS.names()) == {
        "assess_disruption_impact",
        "validate_current_plan",
        "get_traffic_conditions",
        "get_weather_conditions",
    }
    assert DRIVER_COMMS_TOOLS.names() == ("lookup_driver_assignment",)
    assert isinstance(ROUTE_PLANNING_TOOLS, ToolRegistry)
