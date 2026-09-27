"""Case (a) wiring tests (spec task 7 / R5).

When the closure sits on the leg the vehicle is CURRENTLY driving, no mid-leg
diversion is attempted: _reroute_one_vehicle returns None, and in a group the
vehicle carries its original route with no reroute reported. When that is the
only affected vehicle, the group produces nothing to reroute, so the caller
(task 8) falls back to re-time-in-place.
"""

from __future__ import annotations

from mahjourney.api import _reroute_candidate_routes, _reroute_one_vehicle
from mahjourney.domain import Coordinate, Order, PlanVersion, RouteStop, Vehicle, VehicleRoute


def _coord(lat: float, lon: float) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def _rect(lat_lo: float, lat_hi: float, lon_lo: float, lon_hi: float) -> tuple[Coordinate, ...]:
    return (
        _coord(lat_lo, lon_lo), _coord(lat_lo, lon_hi),
        _coord(lat_hi, lon_hi), _coord(lat_hi, lon_lo),
    )


DEPOT = _coord(1.30, 103.80)
VEHICLE = Vehicle(vehicle_id="TRK-A", driver_id="DRV-A", start=DEPOT,
                  working_start_minute=480, working_end_minute=1080)
STOPS = (
    RouteStop(stop_id="A1", sequence=1, location=_coord(1.30, 103.82),
              eta_minute=500, departure_minute=505, demand=1),
    RouteStop(stop_id="A2", sequence=2, location=_coord(1.30, 103.84),
              eta_minute=525, departure_minute=530, demand=1),
    RouteStop(stop_id="A3", sequence=3, location=_coord(1.30, 103.86),
              eta_minute=550, departure_minute=555, demand=1),
)
ROUTE = VehicleRoute(vehicle_id="TRK-A", driver_id="DRV-A", stops=STOPS,
                     distance_km=12.0, duration_minutes=90)
ORDERS = (
    Order(order_id="A1", address="a1", location=_coord(1.30, 103.82)),
    Order(order_id="A2", address="a2", location=_coord(1.30, 103.84)),
    Order(order_id="A3", address="a3", location=_coord(1.30, 103.86)),
)
PLAN = PlanVersion(plan_id="plan-1", version=3, status="ACTIVE",
                   source_data_version="operational-v3:abc",
                   routes=(ROUTE,), objective_cost=12.0)

# At minute 490 the truck is on the depot->A1 leg (lat 1.30, lon 103.80->103.82).
# A closure box on THAT leg makes it case (a): the current road is closed.
CURRENT_LEG_ZONE = _rect(1.299, 1.301, 103.805, 103.815)
MINUTE_ON_FIRST_LEG = 490


def test_reroute_one_vehicle_case_a_returns_none() -> None:
    result = _reroute_one_vehicle(ROUTE, VEHICLE, ORDERS, CURRENT_LEG_ZONE, MINUTE_ON_FIRST_LEG)
    assert result is None


def test_group_case_a_carries_original_and_reports_no_reroute() -> None:
    routes, rerouted = _reroute_candidate_routes(
        PLAN, (VEHICLE,), ORDERS, CURRENT_LEG_ZONE, MINUTE_ON_FIRST_LEG,
        affected_vehicle_ids=("TRK-A",),
    )
    # Original route carried over unchanged; nothing reported as rerouted, so
    # the trigger (task 8) will fall back to re-time-in-place.
    assert routes == (ROUTE,)
    assert rerouted == ()
