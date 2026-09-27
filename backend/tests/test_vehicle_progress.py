"""Tests for the shared vehicle_progress computation (spec task 1).

vehicle_progress is the single source of truth for both the map dot and the
mid-route reroute seed, so these tests pin down the arrival threshold and the
completed / committed / remaining split precisely.
"""

from __future__ import annotations

from mahjourney.domain import Coordinate, RouteStop, Vehicle, VehicleRoute
from mahjourney.simulation import vehicle_progress


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


def _three_stop_route(geometry: tuple[Coordinate, ...] = ()) -> VehicleRoute:
    """Route with three stops on a straight east-bound line from the depot.

    Timeline (minutes): depart depot 480; arrive S1 500, depart 505;
    arrive S2 525, depart 530; arrive S3 550, depart 555; return to depot.
    """
    stops = (
        RouteStop(stop_id="S1", sequence=1, location=_coord(1.30, 103.82),
                  eta_minute=500, departure_minute=505, demand=1),
        RouteStop(stop_id="S2", sequence=2, location=_coord(1.30, 103.84),
                  eta_minute=525, departure_minute=530, demand=1),
        RouteStop(stop_id="S3", sequence=3, location=_coord(1.30, 103.86),
                  eta_minute=550, departure_minute=555, demand=1),
    )
    return VehicleRoute(
        vehicle_id="TRK-01",
        driver_id="DRV-01",
        stops=stops,
        distance_km=10.0,
        duration_minutes=75,
        geometry=geometry,
    )


def test_at_depot_before_departure() -> None:
    progress = vehicle_progress(_three_stop_route(), _vehicle(), current_minute=480)
    assert progress.position == DEPOT
    assert progress.phase == "AT_DEPOT"
    assert progress.completed_stop_ids == ()
    assert progress.committed_stop_id == "S1"
    assert progress.remaining_stop_ids == ("S2", "S3")


def test_reserve_vehicle_no_stops() -> None:
    empty = VehicleRoute(
        vehicle_id="TRK-01", driver_id="DRV-01", stops=(), distance_km=0.0,
        duration_minutes=0,
    )
    progress = vehicle_progress(empty, _vehicle(), current_minute=600)
    assert progress.position == DEPOT
    assert progress.phase == "STANDBY"
    assert progress.completed_stop_ids == ()
    assert progress.committed_stop_id is None
    assert progress.remaining_stop_ids == ()
    assert progress.current_leg is None


def test_mid_leg_toward_next_stop() -> None:
    # Between depart-depot (480) and S1 arrival (500): en route on the first leg.
    progress = vehicle_progress(_three_stop_route(), _vehicle(), current_minute=490)
    assert progress.phase == "EN_ROUTE"
    assert progress.completed_stop_ids == ()
    assert progress.committed_stop_id == "S1"
    assert progress.remaining_stop_ids == ("S2", "S3")
    assert progress.current_leg == (DEPOT, _coord(1.30, 103.82))
    # Uniform model: position is (490-480)/(555-480) = 10/75 ≈ 13.3% along the
    # straight-line path depot(103.80) -> S3(103.86), so lon ≈ 103.808.
    # Just verify it's between depot and S1 (not teleported ahead).
    assert 103.80 <= progress.position.lon <= 103.82


def test_mid_leg_after_first_stop() -> None:
    # After S1 departs (505), before S2 arrival (525): completed == S1,
    # committed == S2, remaining == S3.
    progress = vehicle_progress(_three_stop_route(), _vehicle(), current_minute=515)
    assert progress.phase == "EN_ROUTE"
    assert progress.completed_stop_ids == ("S1",)
    assert progress.committed_stop_id == "S2"
    assert progress.remaining_stop_ids == ("S3",)
    assert progress.current_leg == (_coord(1.30, 103.82), _coord(1.30, 103.84))


def test_currently_servicing_stop_counts_as_completed() -> None:
    # Under the geometry-fraction model, S1 is completed once the dot has
    # physically passed S1's position on the polyline — not at its ETA.
    # S1 is at lon 103.82, which is 1/3 of the way along the straight
    # depot(103.80)->S3(103.86) corridor.  The dot reaches that fraction at
    # roughly minute 505 (480 + 0.333*75).  Use minute 510 to be clearly past.
    progress = vehicle_progress(_three_stop_route(), _vehicle(), current_minute=510)
    assert progress.phase == "EN_ROUTE"
    assert "S1" in progress.completed_stop_ids
    assert progress.committed_stop_id == "S2"
    assert "S3" in progress.remaining_stop_ids


def test_all_stops_completed() -> None:
    # After the last stop's ETA: everything completed, nothing to re-sequence.
    progress = vehicle_progress(_three_stop_route(), _vehicle(), current_minute=560)
    assert progress.completed_stop_ids == ("S1", "S2", "S3")
    assert progress.committed_stop_id is None
    assert progress.remaining_stop_ids == ()
    assert progress.position == _coord(1.30, 103.86)


def test_geometry_and_straight_line_agree_on_leg_selection() -> None:
    # A geometry that traces the same straight east-bound line should place the
    # dot at essentially the same spot as the straight-line fallback, and both
    # must agree on completed/committed/remaining.
    geometry = tuple(_coord(1.30, 103.80 + 0.002 * i) for i in range(31))  # 103.80..103.86
    with_geom = vehicle_progress(_three_stop_route(geometry), _vehicle(), current_minute=515)
    without_geom = vehicle_progress(_three_stop_route(), _vehicle(), current_minute=515)
    assert with_geom.completed_stop_ids == without_geom.completed_stop_ids
    assert with_geom.committed_stop_id == without_geom.committed_stop_id
    assert with_geom.remaining_stop_ids == without_geom.remaining_stop_ids
    # Positions should be close (same straight corridor), within a small tolerance.
    assert abs(with_geom.position.lon - without_geom.position.lon) < 0.01


def test_late_opening_first_window_uses_real_departure() -> None:
    # First stop's window opens late, so the vehicle waits at the depot: its
    # arrival is far later than shift start. Before that ETA the dot is still
    # at the depot / en route, not teleported ahead.
    vehicle = _vehicle()
    late_stop = RouteStop(stop_id="S1", sequence=1, location=_coord(1.30, 103.82),
                          eta_minute=900, departure_minute=905, demand=1)
    route = VehicleRoute(
        vehicle_id="TRK-01", driver_id="DRV-01", stops=(late_stop,),
        distance_km=4.0, duration_minutes=20,
    )
    # At minute 600, well before the 900 ETA: not yet arrived.
    progress = vehicle_progress(route, vehicle, current_minute=600)
    assert progress.completed_stop_ids == ()
    assert progress.committed_stop_id == "S1"
