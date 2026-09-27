"""Tests for leg-level closure avoidance in the solve cost (spec task 4 / R10).

A closure on a road between two stops (neither stop inside the zone) must
influence the solver's sequence so chosen legs avoid the closure when an
alternative ordering exists. With no closures, cost is unchanged. When every
ordering must cross, a soft fallback still yields a candidate.
"""

from __future__ import annotations

from mahjourney.domain import Coordinate, Order, Vehicle
from mahjourney.planning import (
    _build_route,
    _nearest_neighbor,
    _ortools_order,
    leg_crosses_closure,
)


def _coord(lat: float, lon: float) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


DEPOT = _coord(1.30, 103.80)


def _vehicle() -> Vehicle:
    return Vehicle(
        vehicle_id="TRK-01", driver_id="DRV-01", start=DEPOT,
        working_start_minute=480, working_end_minute=1080,
    )


def _rect(lat_lo: float, lat_hi: float, lon_lo: float, lon_hi: float) -> tuple[Coordinate, ...]:
    return (
        _coord(lat_lo, lon_lo), _coord(lat_lo, lon_hi),
        _coord(lat_hi, lon_hi), _coord(lat_hi, lon_lo),
    )


def test_leg_crosses_closure_predicate() -> None:
    # A leg straight east from lon 103.80 to 103.86 passes through a zone
    # centered around lon 103.83; a leg that stays north of it does not.
    zone = _rect(1.299, 1.301, 103.828, 103.832)
    assert leg_crosses_closure(_coord(1.30, 103.80), _coord(1.30, 103.86), (zone,))
    assert not leg_crosses_closure(_coord(1.32, 103.80), _coord(1.32, 103.86), (zone,))
    # No polygons -> never crosses (default cost path pays nothing).
    assert not leg_crosses_closure(_coord(1.30, 103.80), _coord(1.30, 103.86), None)


def test_nearest_neighbor_avoids_closure_when_alternative_exists() -> None:
    # From the depot, ORD-NEAR is closest but the direct leg to it crosses a
    # closure; ORD-DETOUR is slightly farther but reachable without crossing.
    # Avoidance must pick the non-crossing order first.
    vehicle = _vehicle()
    # NEAR is due east and closest by straight-line distance; DETOUR is farther
    # (north-east). Without a closure, NEAR is chosen first.
    near = Order(order_id="ORD-NEAR", address="near", location=_coord(1.30, 103.815))
    detour = Order(order_id="ORD-DETOUR", address="detour", location=_coord(1.315, 103.817))
    orders = [near, detour]
    # A small closure box sits on the due-east depot->NEAR corridor (lat ~1.30,
    # lon ~103.808) but is north-clear of the depot->DETOUR diagonal.
    zone = _rect(1.299, 1.301, 103.806, 103.810)

    # Sanity: the depot->NEAR leg crosses; depot->DETOUR does not.
    assert leg_crosses_closure(vehicle.start, near.location, (zone,))
    assert not leg_crosses_closure(vehicle.start, detour.location, (zone,))

    without = _nearest_neighbor(vehicle, list(orders))
    withz = _nearest_neighbor(vehicle, list(orders), closure_polygons=(zone,))
    # Without closure, distance picks NEAR first; with closure, DETOUR first.
    assert without[0].order_id == "ORD-NEAR"
    assert withz[0].order_id == "ORD-DETOUR"


def test_nearest_neighbor_no_closure_matches_default() -> None:
    vehicle = _vehicle()
    orders = [
        Order(order_id="ORD-A", address="a", location=_coord(1.30, 103.82)),
        Order(order_id="ORD-B", address="b", location=_coord(1.30, 103.84)),
    ]
    baseline = _nearest_neighbor(vehicle, list(orders))
    explicit_none = _nearest_neighbor(vehicle, list(orders), closure_polygons=None)
    assert [o.order_id for o in baseline] == [o.order_id for o in explicit_none]


def test_ortools_no_closure_matches_default() -> None:
    vehicle = _vehicle()
    orders = [
        Order(order_id="ORD-A", address="a", location=_coord(1.30, 103.82)),
        Order(order_id="ORD-B", address="b", location=_coord(1.30, 103.84)),
        Order(order_id="ORD-C", address="c", location=_coord(1.30, 103.86)),
    ]
    baseline = _ortools_order(vehicle, list(orders), time_limit_seconds=1)
    explicit_none = _ortools_order(
        vehicle, list(orders), time_limit_seconds=1, closure_polygons=None
    )
    assert [o.order_id for o in baseline] == [o.order_id for o in explicit_none]


def test_ortools_soft_fallback_when_every_ordering_crosses() -> None:
    # A closure that covers all the orders' locations means every leg into any
    # stop crosses it: hard exclusion can find no closure-free ordering, so the
    # soft fallback must still return a complete sequence (no failure/None).
    vehicle = _vehicle()
    orders = [
        Order(order_id="ORD-A", address="a", location=_coord(1.30, 103.82)),
        Order(order_id="ORD-B", address="b", location=_coord(1.30, 103.83)),
    ]
    # A big zone enclosing the depot and both stops -> unavoidable crossing.
    zone = _rect(1.28, 1.32, 103.78, 103.86)
    for order in orders:
        assert leg_crosses_closure(vehicle.start, order.location, (zone,))
    ordered = _ortools_order(
        vehicle, list(orders), time_limit_seconds=1, closure_polygons=(zone,)
    )
    assert {o.order_id for o in ordered} == {o.order_id for o in orders}


def test_build_route_distance_is_physical_not_penalized() -> None:
    # _build_route must report REAL distance even when a chosen leg crosses a
    # closure (the penalty shapes ordering in the solver, not the reported cost).
    vehicle = _vehicle()
    orders = [Order(order_id="ORD-A", address="a", location=_coord(1.30, 103.82))]
    zone = _rect(1.298, 1.302, 103.805, 103.815)  # on the depot->A leg
    assert leg_crosses_closure(vehicle.start, orders[0].location, (zone,))
    without = _build_route(vehicle, list(orders))
    withz = _build_route(vehicle, list(orders), closure_polygons=(zone,))
    # Same physical route (same single order), so distance is identical and
    # nowhere near the millions-scale penalty constant.
    assert withz.distance_km == without.distance_km
    assert withz.distance_km < 1000
