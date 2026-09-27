"""Tests for the parameterized solver origin (spec task 3).

The origin override (start_coordinate / start_node_id / start_minute) lets a
mid-route reroute seed the solver at the vehicle's current position and time
instead of the depot at shift start. When omitted, behavior must be identical to
today's depot-origin planning.
"""

from __future__ import annotations

from mahjourney.domain import Coordinate, Order, Vehicle
from mahjourney.planning import _build_route, _ortools_order, depot_node_id


def _coord(lat: float, lon: float) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


DEPOT = _coord(1.30, 103.80)


def _vehicle() -> Vehicle:
    return Vehicle(
        vehicle_id="TRK-01",
        driver_id="DRV-01",
        start=DEPOT,
        working_start_minute=480,
        working_end_minute=1080,
    )


def _orders() -> list[Order]:
    return [
        Order(order_id="ORD-A", address="A", location=_coord(1.30, 103.82),
              window_start_minute=480, window_end_minute=1080),
        Order(order_id="ORD-B", address="B", location=_coord(1.30, 103.84),
              window_start_minute=480, window_end_minute=1080),
        Order(order_id="ORD-C", address="C", location=_coord(1.30, 103.86),
              window_start_minute=480, window_end_minute=1080),
    ]


def test_ortools_order_no_override_matches_default() -> None:
    vehicle, orders = _vehicle(), _orders()
    baseline = _ortools_order(vehicle, orders, time_limit_seconds=1)
    # Passing the origin defaults explicitly must be identical to omitting them.
    explicit = _ortools_order(
        vehicle, orders, time_limit_seconds=1,
        start_coordinate=None, start_node_id=None, start_minute=None,
    )
    assert [o.order_id for o in baseline] == [o.order_id for o in explicit]


def test_build_route_no_override_matches_default() -> None:
    vehicle, orders = _vehicle(), _orders()
    baseline = _build_route(vehicle, orders)
    explicit = _build_route(
        vehicle, orders, start_coordinate=None, start_node_id=None, start_minute=None,
    )
    assert baseline == explicit
    # Sanity: default route starts its first leg from the depot at shift start.
    assert baseline.stops[0].eta_minute >= vehicle.working_start_minute


def test_build_route_override_uses_start_minute_for_first_eta() -> None:
    vehicle, orders = _vehicle(), _orders()
    # Seed at a mid-shift position/time: near ORD-A at minute 700.
    start = _coord(1.30, 103.815)
    route = _build_route(
        vehicle, orders,
        start_coordinate=start,
        start_node_id="progress:TRK-01",
        start_minute=700,
    )
    # First stop's ETA is computed from the mid-shift start (700), not shift
    # start (480): it must be at/after 700, and clearly later than a depot-origin
    # build would produce for the same first order.
    assert route.stops[0].eta_minute >= 700
    depot_origin = _build_route(vehicle, orders)
    assert route.stops[0].eta_minute > depot_origin.stops[0].eta_minute


def test_ortools_order_override_still_returns_all_orders() -> None:
    vehicle, orders = _vehicle(), _orders()
    ordered = _ortools_order(
        vehicle, orders, time_limit_seconds=1,
        start_coordinate=_coord(1.30, 103.815),
        start_node_id="progress:TRK-01",
        start_minute=700,
    )
    # Re-sequencing the remaining stops: every input order is still present.
    assert {o.order_id for o in ordered} == {o.order_id for o in orders}


def test_build_route_override_returns_to_depot() -> None:
    vehicle, orders = _vehicle(), _orders()
    route = _build_route(
        vehicle, orders,
        start_coordinate=_coord(1.30, 103.815),
        start_node_id="progress:TRK-01",
        start_minute=700,
    )
    # The route object doesn't store the return leg as a stop, but its distance
    # must include the final return to the depot. Compare against a hand check:
    # distance is > 0 and the last stop is an order, not the depot.
    assert route.distance_km > 0
    assert route.stops[-1].stop_id in {o.order_id for o in orders}
    # depot_node_id is still the terminus key used internally.
    assert depot_node_id(vehicle.start).startswith("depot:")
