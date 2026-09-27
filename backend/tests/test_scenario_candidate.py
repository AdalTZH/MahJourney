"""Task 3: non-persisting candidate generation + geometry/route filtering."""

from collections import defaultdict

from mahjourney.api import _compute_candidate_plan, filter_candidate_routes
from mahjourney.config import Settings
from mahjourney.domain import DisruptionEvent, DisruptionType
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.planning import build_plan
from mahjourney.state import AppState

# A generous bounding box covering the whole synthetic Jurong-area order
# cluster, so any vehicle whose route passes through it is affected. Real
# usage draws a much smaller, targeted zone; the test only needs one that
# reliably intersects at least one real vehicle's stops.
_FLEET_WIDE_ZONE = [
    {"lat": 1.15, "lon": 103.55},
    {"lat": 1.15, "lon": 104.10},
    {"lat": 1.50, "lon": 104.10},
    {"lat": 1.50, "lon": 103.55},
]


def _state() -> AppState:
    # Clear the OneMap token so geometry enrichment (a live network call) is
    # skipped in tests; candidate computation still runs the full planner.
    # AppState no longer seeds synthetic fleet/orders itself (operational data
    # is DB-only in production), so tests build the fixture plan directly and
    # inject it into a fresh AppState without going through initialize()/DB.
    settings = Settings(
        _env_file=None,
        persistence_enabled=False,
        onemap_access_token="",
        road_optimized_routing=False,
    )
    state = AppState(settings)
    # No repository in this test: candidate computation must run purely off
    # in-memory fixture data, without attempting any DB call (there is no
    # live 'db' host available under pytest).
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


def _inject_active_closure(state: AppState) -> None:
    # Effective before the current minute so active_disruptions() picks it up
    # during candidate computation.
    state.simulation.update("demo", current_minute=540)
    event = DisruptionEvent(
        scenario_id="demo",
        event_type=DisruptionType.ROAD_CLOSURE,
        effective_minute=480,
        payload={"polygon": _FLEET_WIDE_ZONE},
    )
    state.simulation.inject(event)


async def test_candidate_generation_does_not_touch_live_plan_history() -> None:
    state = _state()
    _inject_active_closure(state)
    plan_ids_before = {pid: len(v) for pid, v in state.plans.items()}
    latest_before = state.latest_plan.plan_id
    total_versions_before = sum(len(v) for v in state.plans.values())

    candidate = await _compute_candidate_plan(
        state,
        source_data_version="scenario-injection:test",
        parent_plan_id=state.active_plan_id,
    )

    # The candidate must NOT have been appended to state.plans, and latest_plan
    # / map state must be unchanged.
    assert sum(len(v) for v in state.plans.values()) == total_versions_before
    assert {pid: len(v) for pid, v in state.plans.items()} == plan_ids_before
    assert state.latest_plan.plan_id == latest_before
    assert candidate.status in {"CANDIDATE", "VALIDATED"}


async def test_candidate_reflects_the_disruption_and_filters_to_affected_vehicle() -> None:
    state = _state()
    baseline = state.latest_plan
    _inject_active_closure(state)

    candidate = await _compute_candidate_plan(
        state,
        source_data_version="scenario-injection:test",
        parent_plan_id=state.active_plan_id,
    )
    # The candidate was computed under the active disruption speed penalty, so
    # its source_data_version records the disruption.
    assert "disruption:ROAD_CLOSURE" in candidate.source_data_version

    vehicle_id = baseline.routes[0].vehicle_id
    filtered = filter_candidate_routes(candidate, (vehicle_id,))
    assert [route.vehicle_id for route in filtered] == [vehicle_id]

    # The affected vehicle's candidate route should differ from the baseline
    # route in either its stop sequence or its timing/distance under disruption
    # (the zone covers the whole synthetic order cluster, so every vehicle's
    # route is penalized).
    base_route = next(r for r in baseline.routes if r.vehicle_id == vehicle_id)
    cand_route = filtered[0]
    base_seq = [stop.stop_id for stop in base_route.stops]
    cand_seq = [stop.stop_id for stop in cand_route.stops]
    assert (
        cand_seq != base_seq
        or cand_route.duration_minutes != base_route.duration_minutes
        or cand_route.distance_km != base_route.distance_km
    )


async def test_filter_excludes_unaffected_vehicles() -> None:
    state = _state()
    _inject_active_closure(state)
    candidate = await _compute_candidate_plan(
        state, source_data_version="scenario-injection:test", parent_plan_id=None
    )
    vehicle_id = candidate.routes[0].vehicle_id
    filtered = filter_candidate_routes(candidate, (vehicle_id,))
    filtered_ids = {route.vehicle_id for route in filtered}
    other = "TRK-02" if vehicle_id != "TRK-02" else "TRK-01"
    assert other not in filtered_ids
    assert filtered_ids == {vehicle_id}
