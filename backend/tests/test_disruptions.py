"""Tests for mahjourney.disruptions: the point-in-polygon utility used to match
order locations to user-drawn disruption zones, and the payload-driven speed
model built on top of it."""

from mahjourney.disruptions import (
    CLOSURE_SPEED_FACTOR,
    RAIN_SEVERITY_SPEED_FACTOR,
    path_intersects_polygon,
    point_in_polygon,
    speed_factor_for_location,
)
from mahjourney.domain import Coordinate, DisruptionEvent, DisruptionType

# A simple square roughly 0.01 degrees on a side (~1km at Singapore's latitude),
# well within Coordinate's allowed lat/lon range.
_SQUARE = (
    Coordinate(lat=1.30, lon=103.70),
    Coordinate(lat=1.30, lon=103.71),
    Coordinate(lat=1.31, lon=103.71),
    Coordinate(lat=1.31, lon=103.70),
)


def test_point_clearly_inside_polygon_is_contained() -> None:
    center = Coordinate(lat=1.305, lon=103.705)
    assert point_in_polygon(center, _SQUARE) is True


def test_point_clearly_outside_polygon_is_not_contained() -> None:
    far_away = Coordinate(lat=1.40, lon=103.90)
    assert point_in_polygon(far_away, _SQUARE) is False


def test_point_just_outside_each_edge_is_not_contained() -> None:
    assert point_in_polygon(Coordinate(lat=1.305, lon=103.695), _SQUARE) is False  # west
    assert point_in_polygon(Coordinate(lat=1.305, lon=103.715), _SQUARE) is False  # east
    assert point_in_polygon(Coordinate(lat=1.295, lon=103.705), _SQUARE) is False  # south
    assert point_in_polygon(Coordinate(lat=1.315, lon=103.705), _SQUARE) is False  # north


def test_point_just_inside_each_edge_is_contained() -> None:
    assert point_in_polygon(Coordinate(lat=1.305, lon=103.701), _SQUARE) is True
    assert point_in_polygon(Coordinate(lat=1.305, lon=103.709), _SQUARE) is True
    assert point_in_polygon(Coordinate(lat=1.301, lon=103.705), _SQUARE) is True
    assert point_in_polygon(Coordinate(lat=1.309, lon=103.705), _SQUARE) is True


def test_polygon_does_not_need_to_be_explicitly_closed() -> None:
    # _SQUARE's last vertex is not a duplicate of the first; the function
    # should still treat it as a closed ring by wrapping the final edge.
    assert _SQUARE[0] != _SQUARE[-1]
    assert point_in_polygon(Coordinate(lat=1.305, lon=103.705), _SQUARE) is True


def test_degenerate_polygon_with_fewer_than_three_points_is_never_contained() -> None:
    point = Coordinate(lat=1.305, lon=103.705)
    assert point_in_polygon(point, ()) is False
    assert point_in_polygon(point, (Coordinate(lat=1.30, lon=103.70),)) is False
    assert (
        point_in_polygon(
            point, (Coordinate(lat=1.30, lon=103.70), Coordinate(lat=1.31, lon=103.71))
        )
        is False
    )


def test_non_convex_polygon_is_matched_correctly() -> None:
    # A "C" shaped (non-convex) polygon: the notch on the right side should be
    # excluded even though it sits within the overall bounding box.
    notched = (
        Coordinate(lat=1.30, lon=103.70),
        Coordinate(lat=1.30, lon=103.72),
        Coordinate(lat=1.31, lon=103.72),
        Coordinate(lat=1.31, lon=103.715),
        Coordinate(lat=1.305, lon=103.715),
        Coordinate(lat=1.305, lon=103.705),
        Coordinate(lat=1.31, lon=103.705),
        Coordinate(lat=1.31, lon=103.70),
    )
    inside_the_body = Coordinate(lat=1.302, lon=103.71)
    inside_the_notch = Coordinate(lat=1.308, lon=103.71)
    assert point_in_polygon(inside_the_body, notched) is True
    assert point_in_polygon(inside_the_notch, notched) is False


# --- speed_factor_for_location: polygon-scoped disruption effects ---

_INSIDE = Coordinate(lat=1.305, lon=103.705)
_OUTSIDE = Coordinate(lat=1.40, lon=103.90)
_ZONE_POLYGON_PAYLOAD = [
    {"lat": 1.30, "lon": 103.70},
    {"lat": 1.30, "lon": 103.71},
    {"lat": 1.31, "lon": 103.71},
    {"lat": 1.31, "lon": 103.70},
]


def _closure_event(effective_minute: int = 0) -> DisruptionEvent:
    return DisruptionEvent(
        scenario_id="demo",
        event_type=DisruptionType.ROAD_CLOSURE,
        effective_minute=effective_minute,
        payload={"polygon": _ZONE_POLYGON_PAYLOAD},
    )


def _rain_event(severity: str = "HEAVY", effective_minute: int = 0) -> DisruptionEvent:
    return DisruptionEvent(
        scenario_id="demo",
        event_type=DisruptionType.HEAVY_RAIN,
        effective_minute=effective_minute,
        payload={"polygon": _ZONE_POLYGON_PAYLOAD, "severity": severity},
    )


def test_road_closure_only_penalizes_orders_inside_its_zone() -> None:
    closure = (_closure_event(),)
    assert speed_factor_for_location(_INSIDE, closure) == CLOSURE_SPEED_FACTOR
    assert speed_factor_for_location(_OUTSIDE, closure) == 1.0


def test_heavy_rain_only_penalizes_orders_inside_its_zone() -> None:
    # Unlike the old model, rain no longer applies fleet-wide: it is scoped to
    # its drawn zone just like a closure.
    rain = (_rain_event("HEAVY"),)
    assert speed_factor_for_location(_INSIDE, rain) == RAIN_SEVERITY_SPEED_FACTOR["HEAVY"]
    assert speed_factor_for_location(_OUTSIDE, rain) == 1.0


def test_moderate_rain_severity_applies_a_smaller_penalty() -> None:
    rain = (_rain_event("MODERATE"),)
    assert speed_factor_for_location(_INSIDE, rain) == RAIN_SEVERITY_SPEED_FACTOR["MODERATE"]


def test_overlapping_zones_compound_multiplicatively() -> None:
    both = (_closure_event(), _rain_event("HEAVY"))
    expected = CLOSURE_SPEED_FACTOR * RAIN_SEVERITY_SPEED_FACTOR["HEAVY"]
    assert speed_factor_for_location(_INSIDE, both) == expected
    # Outside both zones, neither penalty applies.
    assert speed_factor_for_location(_OUTSIDE, both) == 1.0


def test_missing_polygon_in_payload_has_no_effect() -> None:
    malformed = (
        DisruptionEvent(
            scenario_id="demo",
            event_type=DisruptionType.ROAD_CLOSURE,
            effective_minute=0,
            payload={},
        ),
    )
    assert speed_factor_for_location(_INSIDE, malformed) == 1.0


def test_malformed_polygon_points_are_ignored_without_raising() -> None:
    malformed = (
        DisruptionEvent(
            scenario_id="demo",
            event_type=DisruptionType.ROAD_CLOSURE,
            effective_minute=0,
            payload={"polygon": [{"lat": 1.30}, {"lon": 103.70}]},
        ),
    )
    assert speed_factor_for_location(_INSIDE, malformed) == 1.0


def test_truck_breakdown_and_urgent_order_never_change_speed() -> None:
    # These are excluded from PLANNING_DISRUPTION_TYPES upstream, but even if
    # one reaches speed_factor_for_location directly it should be a no-op.
    non_planning = (
        DisruptionEvent(
            scenario_id="demo",
            event_type=DisruptionType.TRUCK_BREAKDOWN,
            effective_minute=0,
            payload={"polygon": _ZONE_POLYGON_PAYLOAD},
        ),
    )
    assert speed_factor_for_location(_INSIDE, non_planning) == 1.0


def test_path_crossing_polygon_without_any_vertex_inside_is_affected() -> None:
    # A straight west->east path whose endpoints are both outside the square,
    # but which passes straight through it. This is the exact case the old
    # stop-only check missed: the route drives through the zone but no vertex
    # (stop) is inside it.
    path = (
        Coordinate(lat=1.305, lon=103.68),  # west of the square
        Coordinate(lat=1.305, lon=103.73),  # east of the square
    )
    assert not any(point_in_polygon(point, _SQUARE) for point in path)
    assert path_intersects_polygon(path, _SQUARE) is True


def test_path_with_a_vertex_inside_polygon_is_affected() -> None:
    path = (
        Coordinate(lat=1.28, lon=103.705),  # outside (south)
        Coordinate(lat=1.305, lon=103.705),  # inside the square
    )
    assert path_intersects_polygon(path, _SQUARE) is True


def test_path_entirely_outside_polygon_is_not_affected() -> None:
    path = (
        Coordinate(lat=1.28, lon=103.68),
        Coordinate(lat=1.29, lon=103.69),
    )
    assert path_intersects_polygon(path, _SQUARE) is False


def test_path_intersection_handles_degenerate_input() -> None:
    point = Coordinate(lat=1.305, lon=103.705)
    # Degenerate polygon or empty path -> never affected, never raises.
    assert path_intersects_polygon((point, point), ()) is False
    assert path_intersects_polygon((), _SQUARE) is False
    # A single-point path that sits inside the zone still counts.
    assert path_intersects_polygon((point,), _SQUARE) is True
