"""Tests for the group-orchestration pure helpers (spec task 6 / R8).

_reroute_candidate_routes reroutes every affected vehicle independently and
carries everyone else over unchanged; _assemble_reroute_plan builds one candidate
PlanVersion from the result. The state-touching wrapper _reroute_affected_vehicles
needs full app state (DB-only) and is exercised in the e2e/Docker path.
"""

from __future__ import annotations

from mahjourney.api import _assemble_reroute_plan, _reroute_candidate_routes
from mahjourney.domain import Coordinate, Order, PlanVersion, RouteStop, Vehicle, VehicleRoute


def _coord(lat: float, lon: float) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def _rect(lat_lo: float, lat_hi: float, lon_lo: float, lon_hi: float) -> tuple[Coordinate, ...]:
    return (
        _coord(lat_lo, lon_lo), _coord(lat_lo, lon_hi),
        _coord(lat_hi, lon_hi), _coord(lat_hi, lon_lo),
    )


def _vehicle(vid: str, start: Coordinate) -> Vehicle:
    return Vehicle(
        vehicle_id=vid, driver_id=f"DRV-{vid[-1]}", start=start,
        working_start_minute=480, working_end_minute=1080,
    )


def _order(oid: str, lat: float, lon: float) -> Order:
    return Order(order_id=oid, address=oid, location=_coord(lat, lon),
                 window_start_minute=480, window_end_minute=1080)


# Vehicle A (depot 1.30,103.80) with 3 remaining-capable stops east.
VA = _vehicle("TRK-A", _coord(1.30, 103.80))
VB = _vehicle("TRK-B", _coord(1.32, 103.80))
FLEET = (VA, VB)

A_STOPS = (
    RouteStop(stop_id="A1", sequence=1, location=_coord(1.30, 103.82),
              eta_minute=500, departure_minute=505, demand=1),
    RouteStop(stop_id="A2", sequence=2, location=_coord(1.30, 103.84),
              eta_minute=525, departure_minute=530, demand=1),
    RouteStop(stop_id="A3", sequence=3, location=_coord(1.30, 103.86),
              eta_minute=550, departure_minute=555, demand=1),
    RouteStop(stop_id="A4", sequence=4, location=_coord(1.30, 103.88),
              eta_minute=600, departure_minute=605, demand=1),
)
B_STOPS = (
    RouteStop(stop_id="B1", sequence=1, location=_coord(1.32, 103.82),
              eta_minute=500, departure_minute=505, demand=1),
    RouteStop(stop_id="B2", sequence=2, location=_coord(1.32, 103.84),
              eta_minute=525, departure_minute=530, demand=1),
    RouteStop(stop_id="B3", sequence=3, location=_coord(1.32, 103.86),
              eta_minute=550, departure_minute=555, demand=1),
)
ROUTE_A = VehicleRoute(vehicle_id="TRK-A", driver_id="DRV-A", stops=A_STOPS,
                       distance_km=12.0, duration_minutes=90)
ROUTE_B = VehicleRoute(vehicle_id="TRK-B", driver_id="DRV-B", stops=B_STOPS,
                       distance_km=8.0, duration_minutes=60)
PLAN = PlanVersion(
    plan_id="plan-1", version=3, status="ACTIVE", source_data_version="operational-v3:abc",
    routes=(ROUTE_A, ROUTE_B), objective_cost=20.0,
)
ORDERS = (
    _order("A1", 1.30, 103.82), _order("A2", 1.30, 103.84),
    _order("A3", 1.30, 103.86), _order("A4", 1.30, 103.88),
    _order("B1", 1.32, 103.82), _order("B2", 1.32, 103.84), _order("B3", 1.32, 103.86),
)


def test_only_affected_vehicles_rerouted_others_unchanged() -> None:
    # Closure affects vehicle A's remaining route (a leg past its committed stop);
    # only A is in the affected set. B must carry over byte-identical.
    zone = _rect(1.299, 1.301, 103.848, 103.852)  # on A2->A3
    current_minute = 515  # A: A1 done, A2 committed, A3 remaining
    routes, rerouted = _reroute_candidate_routes(
        PLAN, FLEET, ORDERS, zone, current_minute,
        affected_vehicle_ids=("TRK-A",),
    )
    by_id = {r.vehicle_id: r for r in routes}
    # B unchanged (same object contents).
    assert by_id["TRK-B"] == ROUTE_B
    # A rerouted and reported.
    assert "TRK-A" in rerouted
    assert "TRK-B" not in rerouted
    # A still visits all its stops.
    assert {s.stop_id for s in by_id["TRK-A"].stops} == {"A1", "A2", "A3", "A4"}


def test_multiple_affected_vehicles_all_rerouted() -> None:
    current_minute = 515
    zone = _rect(1.28, 1.34, 103.90, 103.92)  # far from both -> valid reroute, both affected
    routes, rerouted = _reroute_candidate_routes(
        PLAN, FLEET, ORDERS, zone, current_minute,
        affected_vehicle_ids=("TRK-A", "TRK-B"),
    )
    # Both vehicles produced a reroute candidate (each has remaining stops).
    assert set(rerouted) == {"TRK-A", "TRK-B"}
    assert len(routes) == 2


def test_affected_vehicle_that_falls_back_carries_original() -> None:
    # At minute 540: B has B1(500),B2(525) done and B3(550) committed -> no stop
    # after the committed one -> no remaining -> _reroute_one_vehicle returns
    # None, so B carries its original route and is not reported as rerouted.
    # A has A1,A2 done, A3 committed, A4 still remaining -> A reroutes
    # independently (R8.5: one vehicle's fallback doesn't block the other).
    zone = _rect(1.28, 1.34, 103.90, 103.92)
    current_minute = 540
    routes, rerouted = _reroute_candidate_routes(
        PLAN, FLEET, ORDERS, zone, current_minute,
        affected_vehicle_ids=("TRK-A", "TRK-B"),
    )
    by_id = {r.vehicle_id: r for r in routes}
    assert by_id["TRK-B"] == ROUTE_B
    assert "TRK-B" not in rerouted
    assert "TRK-A" in rerouted


def test_assemble_reroute_plan_validated_and_versioned() -> None:
    routes, _ = _reroute_candidate_routes(
        PLAN, FLEET, ORDERS, _rect(1.28, 1.34, 103.90, 103.92), 515,
        affected_vehicle_ids=("TRK-A",),
    )
    candidate = _assemble_reroute_plan(
        routes, FLEET, ORDERS,
        plan_id="plan-1", version=4,
        source_data_version="mid-route-reroute:evt-1+operational-v3:abc",
    )
    assert candidate.plan_id == "plan-1"
    assert candidate.version == 4
    assert candidate.status in {"CANDIDATE", "VALIDATED"}
    assert candidate.source_data_version.startswith("mid-route-reroute:evt-1")
    # objective_cost is the sum of route distances.
    assert candidate.objective_cost == round(sum(r.distance_km for r in routes), 2)
