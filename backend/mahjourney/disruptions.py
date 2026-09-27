"""Turn active disruption events into real travel-speed penalties.

Only :class:`~mahjourney.domain.DisruptionType` values that are genuine planning
factors are handled here: ``ROAD_CLOSURE`` and ``HEAVY_RAIN``. ``TRUCK_BREAKDOWN``
and ``URGENT_ORDER`` are scenario-lab concepts, not planning inputs, and are
intentionally ignored — including them would require deciding how to reassign a
disrupted vehicle's stops, which is a separate, larger change.

Both disruption types are user-drawn polygon zones (``event.payload["polygon"]``,
a list of ``{"lat": float, "lon": float}`` points, at least 3, forming a ring —
see :func:`point_in_polygon`). An order is only affected if its location falls
inside the zone: a road closure applies a flat slowdown to any order inside its
zone (there is no distance-based taper, since an arbitrary polygon has no single
"center" to taper from — this is a deliberate simplification versus the old
point+radius model); heavy rain applies a slowdown, scaled by severity, to any
order inside its zone. Effects compound multiplicatively when multiple zones
cover the same stop. An event with a missing or malformed polygon is skipped
(has no effect) rather than raising, since disruption payloads are free-form.
This produces a real, deterministic added-time figure per order rather than a
guess.
"""

from __future__ import annotations

import math
from typing import Any

from .domain import Coordinate, DisruptionEvent, DisruptionType, Order

BASE_SPEED_KPH = 28.0

# Default half-width (metres) of the corridor built around a selected road when
# a road-closure disruption is described by a road_path rather than an explicit
# polygon. A thin corridor is enough to catch stops/routes sitting on the road.
DEFAULT_ROAD_BUFFER_M = 40.0

# Flat slowdown applied to any order inside an active road-closure zone.
CLOSURE_SPEED_FACTOR = 0.4

# Slowdown applied to any order inside an active heavy-rain zone, scaled by
# severity.
RAIN_SEVERITY_SPEED_FACTOR = {"MODERATE": 0.88, "HEAVY": 0.75}
DEFAULT_RAIN_SEVERITY = "HEAVY"

# Disruption types that are real planning inputs. TRUCK_BREAKDOWN and
# URGENT_ORDER are deliberately excluded (see module docstring).
PLANNING_DISRUPTION_TYPES = (DisruptionType.ROAD_CLOSURE, DisruptionType.HEAVY_RAIN)


def active_disruptions(
    events: tuple[DisruptionEvent, ...], current_minute: int
) -> tuple[DisruptionEvent, ...]:
    """Planning-relevant disruptions whose effective time has already passed.

    There is no "cleared" state yet, so an event is considered active from its
    effective minute onward for the current scenario day.
    """
    return tuple(
        event
        for event in events
        if event.event_type in PLANNING_DISRUPTION_TYPES
        and event.effective_minute <= current_minute
    )


def point_in_polygon(point: Coordinate, polygon: tuple[Coordinate, ...]) -> bool:
    """Whether ``point`` lies inside ``polygon`` using the ray-casting algorithm.

    ``polygon`` is a ring of vertices in either winding order; it does not need
    to be explicitly closed (the last vertex is implicitly connected back to
    the first). Coordinates are treated as planar (lat/lon as if they were
    plain x/y), which is an acceptable approximation at the scale of a single
    disruption zone drawn on a city map — the same approximation already used
    by :func:`~mahjourney.planning.haversine_km` elsewhere in this module.

    Returns ``False`` for a degenerate polygon (fewer than 3 vertices) rather
    than raising, so a malformed/missing zone in a disruption payload simply
    has no effect instead of breaking plan computation.

    A point exactly on an edge may be classified either way depending on
    floating-point rounding — this is a standard limitation of ray-casting and
    is not treated specially, since disruption zones are approximate by
    nature.
    """
    if len(polygon) < 3:
        return False
    x, y = point.lon, point.lat
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i].lon, polygon[i].lat
        x2, y2 = polygon[(i + 1) % n].lon, polygon[(i + 1) % n].lat
        # Does the horizontal ray from (x, y) going in +x direction cross this edge?
        if (y1 > y) != (y2 > y):
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_intersect:
                inside = not inside
    return inside


def _segments_intersect(
    p1: Coordinate, p2: Coordinate, p3: Coordinate, p4: Coordinate
) -> bool:
    """Whether segment p1->p2 crosses segment p3->p4.

    Uses the standard orientation (signed-area) test. Coordinates are treated
    as planar lon/lat, the same approximation as :func:`point_in_polygon`.
    Collinear-overlap cases are treated as intersecting, which is the
    conservative choice for "does this path touch the zone".
    """

    def orient(a: Coordinate, b: Coordinate, c: Coordinate) -> float:
        return (b.lon - a.lon) * (c.lat - a.lat) - (b.lat - a.lat) * (c.lon - a.lon)

    def on_segment(a: Coordinate, b: Coordinate, c: Coordinate) -> bool:
        # c is known collinear with a->b; is it within the bounding box?
        return (
            min(a.lon, b.lon) <= c.lon <= max(a.lon, b.lon)
            and min(a.lat, b.lat) <= c.lat <= max(a.lat, b.lat)
        )

    d1 = orient(p3, p4, p1)
    d2 = orient(p3, p4, p2)
    d3 = orient(p1, p2, p3)
    d4 = orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    if d1 == 0 and on_segment(p3, p4, p1):
        return True
    if d2 == 0 and on_segment(p3, p4, p2):
        return True
    if d3 == 0 and on_segment(p1, p2, p3):
        return True
    if d4 == 0 and on_segment(p1, p2, p4):
        return True
    return False


def path_intersects_polygon(
    path: tuple[Coordinate, ...], polygon: tuple[Coordinate, ...]
) -> bool:
    """Whether a polyline ``path`` enters or crosses ``polygon``.

    True if any point of the path is inside the polygon, or any path segment
    crosses any polygon edge. This is what makes a vehicle count as "affected"
    when its road path passes through a disruption zone even though none of its
    stops fall inside the zone — e.g. a route that merely drives through a
    heavy-rain area on the way to deliveries elsewhere.

    Returns ``False`` for a degenerate polygon (fewer than 3 vertices) or an
    empty/one-point path, matching :func:`point_in_polygon`'s tolerance for
    malformed input.
    """
    if len(polygon) < 3 or len(path) == 0:
        return False
    # Any vertex of the path inside the zone -> affected. This also covers a
    # path that starts or ends inside the zone without any edge crossing.
    if any(point_in_polygon(point, polygon) for point in path):
        return True
    if len(path) < 2:
        return False
    n = len(polygon)
    for i in range(len(path) - 1):
        seg_start, seg_end = path[i], path[i + 1]
        for j in range(n):
            edge_start = polygon[j]
            edge_end = polygon[(j + 1) % n]
            if _segments_intersect(seg_start, seg_end, edge_start, edge_end):
                return True
    return False


def buffer_path_to_polygon(
    path: tuple[Coordinate, ...], buffer_m: float
) -> tuple[Coordinate, ...] | None:
    """Turn a road polyline into a thin corridor polygon around it.

    Offsets each segment of ``path`` perpendicularly by ``buffer_m`` metres on
    both sides and stitches the offsets into a single ring: the left offsets
    walked forward along the path, then the right offsets walked back, forming
    a closed corridor that hugs the road. This is what lets a selected road
    (start->end polyline) act as a disruption zone via the existing
    polygon-containment machinery (:func:`point_in_polygon`,
    :func:`path_intersects_polygon`).

    Coordinates are treated as planar lon/lat, the same approximation used
    elsewhere in this module; the metre offset is converted to degrees using a
    local latitude scale, which is accurate enough at the scale of a single
    road on a city map.

    Returns ``None`` for a degenerate path (fewer than 2 points) or a
    non-positive buffer, so a malformed road_path payload simply has no effect
    rather than raising.
    """
    if len(path) < 2 or buffer_m <= 0:
        return None
    # Metres-per-degree conversions. Latitude is ~constant; longitude shrinks
    # with the cosine of latitude. Use the path's mid latitude as the local scale.
    mid_lat = sum(point.lat for point in path) / len(path)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    if m_per_deg_lon == 0:
        return None
    buffer_lat = buffer_m / m_per_deg_lat
    buffer_lon = buffer_m / m_per_deg_lon

    left: list[Coordinate] = []
    right: list[Coordinate] = []
    for index in range(len(path) - 1):
        start, end = path[index], path[index + 1]
        # Segment direction in degrees; skip zero-length segments.
        dx = end.lon - start.lon
        dy = end.lat - start.lat
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        # Perpendicular unit vector (normalized in degree space), scaled to the
        # per-axis buffer so the corridor width is ~buffer_m on the ground.
        nx = -dy / length
        ny = dx / length
        offset_lon = nx * buffer_lon
        offset_lat = ny * buffer_lat
        for point in (start, end):
            left.append(Coordinate(lat=point.lat + offset_lat, lon=point.lon + offset_lon))
            right.append(Coordinate(lat=point.lat - offset_lat, lon=point.lon - offset_lon))
    if len(left) < 2:
        return None
    # Left side forward + right side reversed = a single closed ring.
    ring = tuple(left) + tuple(reversed(right))
    return ring if len(ring) >= 3 else None


def _polygon_from_payload(payload: dict[str, Any]) -> tuple[Coordinate, ...] | None:
    """Parse a disruption payload into the zone polygon, or None if
    missing/malformed. Never raises: a bad payload simply has no effect.

    Prefers an explicit ``payload["polygon"]`` (a ring of >=3 points). When no
    polygon is present, falls back to building a thin corridor polygon from
    ``payload["road_path"]`` (a polyline of >=2 points) buffered by
    ``payload["buffer_m"]`` (defaulting to :data:`DEFAULT_ROAD_BUFFER_M`) — this
    is how a road-closure described by a selected road becomes a real zone.
    """
    raw = payload.get("polygon")
    if isinstance(raw, (list, tuple)):
        try:
            points = tuple(
                Coordinate(lat=float(p["lat"]), lon=float(p["lon"])) for p in raw
            )
        except (KeyError, TypeError, ValueError):
            points = ()
        if len(points) >= 3:
            return points
    road_raw = payload.get("road_path")
    if isinstance(road_raw, (list, tuple)):
        try:
            path = tuple(
                Coordinate(lat=float(p["lat"]), lon=float(p["lon"])) for p in road_raw
            )
        except (KeyError, TypeError, ValueError):
            return None
        try:
            buffer_m = float(payload.get("buffer_m", DEFAULT_ROAD_BUFFER_M))
        except (TypeError, ValueError):
            buffer_m = DEFAULT_ROAD_BUFFER_M
        return buffer_path_to_polygon(path, buffer_m)
    return None


def speed_factor_for_location(
    location: Coordinate, disruptions: tuple[DisruptionEvent, ...]
) -> float:
    """Combined [0, 1] speed multiplier at a location from all active disruptions.

    Only disruptions whose zone (``payload["polygon"]``) contains ``location``
    apply their penalty; events with a missing/malformed polygon, or whose
    zone does not cover this location, contribute no penalty.
    """
    factor = 1.0
    for event in disruptions:
        polygon = _polygon_from_payload(event.payload)
        if polygon is None or not point_in_polygon(location, polygon):
            continue
        if event.event_type == DisruptionType.ROAD_CLOSURE:
            factor *= CLOSURE_SPEED_FACTOR
        elif event.event_type == DisruptionType.HEAVY_RAIN:
            severity = str(event.payload.get("severity", DEFAULT_RAIN_SEVERITY)).upper()
            factor *= RAIN_SEVERITY_SPEED_FACTOR.get(severity, RAIN_SEVERITY_SPEED_FACTOR["HEAVY"])
    return factor


def disruption_speed_kph_by_stop(
    orders: tuple[Order, ...],
    disruptions: tuple[DisruptionEvent, ...],
    base_speed_kph_by_stop: dict[str, float] | None = None,
) -> dict[str, float] | None:
    """Per-order travel speed reflecting active disruptions.

    Starts from ``base_speed_kph_by_stop`` (for example live LTA speed-band
    context) when supplied, so disruption penalties compound on top of already
    observed traffic conditions rather than replacing them. Returns ``None``
    when there are no active disruptions and no base context, matching the
    "no traffic context" default used elsewhere in planning.
    """
    if not disruptions:
        return base_speed_kph_by_stop
    speeds: dict[str, float] = {}
    for order in orders:
        base = (base_speed_kph_by_stop or {}).get(order.order_id, BASE_SPEED_KPH)
        speeds[order.order_id] = max(
            5.0, base * speed_factor_for_location(order.location, disruptions)
        )
    return speeds
