"""Tests for _reroute_one_vehicle (spec task 5).

Pure function: given one vehicle's current route, the full order set, a closure
polygon, and the scenario minute, it returns the rerouted VehicleRoute (completed
+ committed stops pinned, remaining stops re-sequenced around the closure) or
None to fall back to re-time-in-place.
"""

from __future__ import annotations

from mahjourney.api import _reroute_one_vehicle
from mahjourney.domain import Coordinate, Order, RouteStop, Vehicle, VehicleRoute


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


def _order(oid: str, lat: float, lon: float) -> Order:
    return Order(order_id=oid, address=oid, location=_coord(lat, lon),
                 window_start_minute=480, window_end_minute=1080)


def _route_with(stops: tuple[RouteStop, ...]) -> VehicleRoute:
    return VehicleRoute(
        vehicle_id="TRK-01", driver_id="DRV-01", stops=stops,
        distance_km=10.0, duration_minutes=90,
    )


# Four stops east of the depot: S1..S4. Timeline lets us place the truck.
STOPS = (
    RouteStop(stop_id="S1", sequence=1, location=_coord(1.30, 103.82),
              eta_minute=500, departure_minute=505, demand=1),
    RouteStop(stop_id="S2", sequence=2, location=_coord(1.30, 103.84),
              eta_minute=525, departure_minute=530, demand=1),
    RouteStop(stop_id="S3", sequence=3, location=_coord(1.30, 103.86),
              eta_minute=550, departure_minute=555, demand=1),
    RouteStop(stop_id="S4", sequence=4, location=_coord(1.315, 103.845),
              eta_minute=575, departure_minute=580, demand=1),
)
ORDERS = (
    _order("S1", 1.30, 103.82),
    _order("S2", 1.30, 103.84),
    _order("S3", 1.30, 103.86),
    _order("S4", 1.315, 103.845),
)
# A closure far from any current leg, used for the "no reroute needed to skip"
# cases; a specific closure per test where geometry matters.
FAR_ZONE = _rect(1.34, 1.36, 103.90, 103.92)


def test_no_remaining_stops_returns_none() -> None:
    # At minute 600, all four stops are past their ETA -> completed; nothing to
    # re-sequence.
    result = _reroute_one_vehicle(_route_with(STOPS), _vehicle(), ORDERS, FAR_ZONE, 600)
    assert result is None


def test_case_a_current_leg_crosses_closure_returns_none() -> None:
    # At minute 490 the truck is on the depot->S1 leg (lat 1.30, lon 103.80->103.82).
    # A closure box on that leg means the current road is closed -> no reroute.
    on_current_leg = _rect(1.299, 1.301, 103.805, 103.815)
    result = _reroute_one_vehicle(_route_with(STOPS), _vehicle(), ORDERS, on_current_leg, 490)
    assert result is None


def test_case_b_pins_completed_and_committed_resequences_tail() -> None:
    # At minute 515: S1 completed (dep 505), committed = S2 (en route), remaining
    # = S3, S4. A closure sits on the S2->S3 straight leg but not elsewhere, so
    # the tail should be re-sequenced to avoid entering S3 directly from S2 when
    # an alternative (via S4) exists.
    # Place a closure between S2 (1.30,103.84) and S3 (1.30,103.86): box at lon ~103.85.
    zone = _rect(1.299, 1.301, 103.848, 103.852)
    result = _reroute_one_vehicle(_route_with(STOPS), _vehicle(), ORDERS, zone, 515)
    assert result is not None
    seq_ids = [s.stop_id for s in result.stops]
    # Completed S1 and committed S2 stay pinned, in order, at the front.
    assert seq_ids[0] == "S1"
    assert seq_ids[1] == "S2"
    # All original stops are still present (v1 assumes all remaining fit).
    assert set(seq_ids) == {"S1", "S2", "S3", "S4"}
    # Sequence numbers are contiguous 1..N.
    assert [s.sequence for s in result.stops] == [1, 2, 3, 4]


def test_case_b_returns_all_remaining_stops() -> None:
    # No closure on any leg: still a valid reroute that keeps every stop.
    zone = FAR_ZONE
    result = _reroute_one_vehicle(_route_with(STOPS), _vehicle(), ORDERS, zone, 515)
    assert result is not None
    assert {s.stop_id for s in result.stops} == {"S1", "S2", "S3", "S4"}


def test_unresolvable_remaining_order_falls_back_to_none() -> None:
    # If a remaining stop id has no matching Order (data mismatch), do not drop
    # it silently — fall back to re-time-in-place (None).
    orders_missing_s4 = tuple(o for o in ORDERS if o.order_id != "S4")
    result = _reroute_one_vehicle(_route_with(STOPS), _vehicle(), orders_missing_s4, FAR_ZONE, 515)
    assert result is None
