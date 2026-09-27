"""Tests for the polygon-based disruption preview/apply endpoints (Task 5)."""

from collections import defaultdict
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mahjourney.api import (
    PolygonDisruptionRequest,
    apply_scenario_disruption,
    preview_scenario_disruption,
)
from mahjourney.config import Settings
from mahjourney.domain import Coordinate, DisruptionType
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.planning import build_plan
from mahjourney.state import AppState

# A generous bounding box covering the whole synthetic Jurong-area order
# cluster, so at least one real vehicle is reliably affected.
_FLEET_WIDE_ZONE = [
    Coordinate(lat=1.15, lon=103.55),
    Coordinate(lat=1.15, lon=104.10),
    Coordinate(lat=1.50, lon=104.10),
    Coordinate(lat=1.50, lon=103.55),
]
# A zone far from every synthetic order, guaranteed to affect nothing.
_EMPTY_ZONE = [
    Coordinate(lat=1.20, lon=103.90),
    Coordinate(lat=1.20, lon=103.95),
    Coordinate(lat=1.22, lon=103.95),
    Coordinate(lat=1.22, lon=103.90),
]


def _state() -> AppState:
    settings = Settings(
        _env_file=None,
        persistence_enabled=False,
        onemap_access_token="",
        road_optimized_routing=False,
    )
    state = AppState(settings)
    # No repository/DB call in these tests; run purely off in-memory fixtures.
    state.repository = None
    state.fleet = synthetic_fleet()
    state.orders = synthetic_orders()
    initial = build_plan(
        state.fleet,
        state.orders,
        max_stops_per_vehicle=settings.max_stops_per_vehicle,
        enforce_delivery_windows=settings.enforce_delivery_windows,
    )
    state.plans = defaultdict(list)
    state.plans[initial.plan_id].append(initial)
    return state


def _request(state: AppState) -> SimpleNamespace:
    # api.app_state(request) only ever does request.app.state.services, so a
    # minimal stand-in avoids needing a real FastAPI Request/TestClient (and
    # therefore avoids needing a live database connection in these tests).
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=state)))


async def test_preview_does_not_mutate_scenario_or_plan_state() -> None:
    state = _state()
    events_before = state.simulation.events("demo")
    plans_before = {pid: len(v) for pid, v in state.plans.items()}
    latest_before = state.latest_plan.plan_id

    body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.ROAD_CLOSURE,
        polygon=_FLEET_WIDE_ZONE,
        effective_minute=480,
    )
    result = await preview_scenario_disruption("demo", body, _request(state))

    assert state.simulation.events("demo") == events_before
    assert {pid: len(v) for pid, v in state.plans.items()} == plans_before
    assert state.latest_plan.plan_id == latest_before
    assert result["affected"]["vehicle_ids"]
    assert result["candidate_routes"]


async def test_preview_reports_no_affected_vehicles_for_an_empty_zone() -> None:
    state = _state()
    body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.ROAD_CLOSURE,
        polygon=_EMPTY_ZONE,
        effective_minute=480,
    )
    result = await preview_scenario_disruption("demo", body, _request(state))
    assert result["affected"]["vehicle_ids"] == ()
    assert result["affected"]["order_ids"] == ()
    assert result["candidate_routes"] == []


async def test_apply_injects_event_and_updates_the_live_plan() -> None:
    state = _state()
    latest_before = state.latest_plan

    body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.ROAD_CLOSURE,
        polygon=_FLEET_WIDE_ZONE,
        effective_minute=0,  # already active at the clock's default minute
    )
    result = await apply_scenario_disruption("demo", body, _request(state))

    events = state.simulation.events("demo")
    assert len(events) == 1
    assert events[0].event_type == DisruptionType.ROAD_CLOSURE
    assert events[0].payload["polygon"]

    # The live plan must actually have been re-timed (a new version appended),
    # not just previewed.
    assert state.latest_plan.plan_id != latest_before.plan_id or (
        state.latest_plan.version > latest_before.version
    )
    assert result["affected"]["vehicle_ids"]
    assert result["event"].event_id == events[0].event_id


async def test_apply_heavy_rain_defaults_severity_and_scopes_to_its_zone() -> None:
    state = _state()
    body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.HEAVY_RAIN,
        polygon=_FLEET_WIDE_ZONE,
        effective_minute=0,
    )
    result = await apply_scenario_disruption("demo", body, _request(state))
    events = state.simulation.events("demo")
    assert events[0].payload["severity"] == "HEAVY"
    assert result["affected"]["vehicle_ids"]


async def test_unsupported_disruption_type_is_rejected_by_preview_and_apply() -> None:
    state = _state()
    body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.TRUCK_BREAKDOWN,
        polygon=_FLEET_WIDE_ZONE,
        effective_minute=0,
    )
    with pytest.raises(HTTPException) as preview_exc:
        await preview_scenario_disruption("demo", body, _request(state))
    assert preview_exc.value.status_code == 400

    with pytest.raises(HTTPException) as apply_exc:
        await apply_scenario_disruption("demo", body, _request(state))
    assert apply_exc.value.status_code == 400
    # Rejection must happen before anything is injected.
    assert state.simulation.events("demo") == ()


async def test_applying_two_distinct_zones_of_the_same_type_each_retime_the_plan() -> None:
    # Regression test for the dedup-by-type-tag bug: two different
    # ROAD_CLOSURE zones must each trigger a re-time, not just the first.
    state = _state()
    first_body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.ROAD_CLOSURE,
        polygon=_FLEET_WIDE_ZONE,
        effective_minute=0,
    )
    await apply_scenario_disruption("demo", first_body, _request(state))
    after_first = state.latest_plan

    second_zone = [
        Coordinate(lat=1.25, lon=103.60),
        Coordinate(lat=1.25, lon=103.65),
        Coordinate(lat=1.27, lon=103.65),
        Coordinate(lat=1.27, lon=103.60),
    ]
    second_body = PolygonDisruptionRequest(
        disruption_type=DisruptionType.ROAD_CLOSURE,
        polygon=second_zone,
        effective_minute=0,
    )
    await apply_scenario_disruption("demo", second_body, _request(state))
    after_second = state.latest_plan

    assert len(state.simulation.events("demo")) == 2
    # The second injection must have produced a newer plan version, not been
    # silently skipped as "already applied" (the old type-tag dedup bug).
    assert after_second.version > after_first.version
