"""Regression tests for the /map/state truck view after it was switched to the
shared vehicle_progress computation (spec task 2).

The switch intentionally unifies the position/completed model on vehicle_progress,
removing the old dual-model contradiction (per-stop interpolation overwritten by a
whole-route hardcoded-480 interpolation). These tests exercise the pure per-truck
assembly helper ``_truck_view`` directly (no app/DB needed — operational data is
DB-only, so full-app TestClient tests only run under the Docker/uv harness). They
assert the response shape is preserved and that every field equals what
vehicle_progress derives for the same route and minute — i.e. the map and the
reroute seed share one source.
"""

from __future__ import annotations

from mahjourney.api import _truck_view
from mahjourney.domain import Coordinate, RouteStop, Vehicle, VehicleRoute
from mahjourney.simulation import vehicle_progress


def _coord(lat: float, lon: float) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def _vehicle(vehicle_id: str = "TRK-01", depot_id: str = "DEP-A",
             start: Coordinate | None = None) -> Vehicle:
    return Vehicle(
        vehicle_id=vehicle_id,
        driver_id=f"DRV-{vehicle_id[-1]}",
        depot_id=depot_id,
        start=start or _coord(1.30, 103.80),
        working_start_minute=480,
        working_end_minute=1080,
    )


def _route(vehicle: Vehicle) -> VehicleRoute:
    stops = (
        RouteStop(stop_id="S1", sequence=1, location=_coord(1.30, 103.82),
                  eta_minute=500, departure_minute=505, demand=1),
        RouteStop(stop_id="S2", sequence=2, location=_coord(1.30, 103.84),
                  eta_minute=525, departure_minute=530, demand=1),
    )
    return VehicleRoute(
        vehicle_id=vehicle.vehicle_id, driver_id=vehicle.driver_id,
        stops=stops, distance_km=6.0, duration_minutes=50,
    )


def test_truck_view_shape_and_matches_vehicle_progress() -> None:
    vehicle = _vehicle()
    route = _route(vehicle)
    minute = 515  # after S1 departs, en route to S2
    view = _truck_view(route, vehicle, vehicle, minute)

    # Response shape preserved.
    assert set(view) == {
        "vehicle_id", "driver_id", "depot_id",
        "position", "phase", "completed_stops", "total_stops",
    }
    assert view["vehicle_id"] == "TRK-01"
    assert view["driver_id"] == route.driver_id
    assert view["depot_id"] == "DEP-A"
    assert view["total_stops"] == 2

    # Every value equals the single shared source of truth.
    expected = vehicle_progress(route, vehicle, minute)
    assert view["phase"] == expected.phase
    assert view["completed_stops"] == expected.completed_count
    assert view["position"] == expected.position


def test_truck_view_completed_uses_arrival_threshold() -> None:
    # Under the geometry-fraction model, a stop is completed once the dot has
    # passed its position on the polyline, not at its ETA.
    # S1 is at ~1/3 of the straight depot->S2 corridor; the dot passes it
    # at roughly minute 505.  Use 515 (after S1 departs) to be clearly past.
    vehicle = _vehicle()
    route = _route(vehicle)
    view = _truck_view(route, vehicle, vehicle, 515)
    assert view["phase"] == "EN_ROUTE"
    assert view["completed_stops"] == 1


def test_truck_view_multi_depot_uses_own_vehicle_depot() -> None:
    # Two vehicles at different depots: each truck view reflects its OWN depot
    # and its own start position before departure (no cross-vehicle leakage).
    a = _vehicle("TRK-1", "DEP-A", _coord(1.30, 103.80))
    b = _vehicle("TRK-2", "DEP-B", _coord(1.35, 103.90))
    route_a, route_b = _route(a), _route(b)

    view_a = _truck_view(route_a, a, a, 0)
    view_b = _truck_view(route_b, b, b, 0)

    assert view_a["depot_id"] == "DEP-A"
    assert view_b["depot_id"] == "DEP-B"
    assert view_a["position"] == a.start
    assert view_b["position"] == b.start
    assert view_a["completed_stops"] == 0
    assert view_b["completed_stops"] == 0


def test_truck_view_reserve_vehicle_no_stops() -> None:
    vehicle = _vehicle()
    empty = VehicleRoute(vehicle_id=vehicle.vehicle_id, driver_id=vehicle.driver_id,
                         stops=(), distance_km=0.0, duration_minutes=0)
    view = _truck_view(empty, vehicle, vehicle, 600)
    assert view["phase"] == "STANDBY"
    assert view["total_stops"] == 0
    assert view["completed_stops"] == 0
    assert view["position"] == vehicle.start


def test_truck_view_fallback_vehicle_when_route_vehicle_missing() -> None:
    # Defensive: route's vehicle not in fleet map => None passed; fallback supplies
    # depot origin, and depot_id is blank (unknown vehicle).
    vehicle = _vehicle()
    route = _route(vehicle)
    view = _truck_view(route, None, vehicle, 0)
    assert view["depot_id"] == ""
    assert view["position"] == vehicle.start
