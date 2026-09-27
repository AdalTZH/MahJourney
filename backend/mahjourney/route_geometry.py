from __future__ import annotations

import asyncio
import math

import httpx

from .domain import Coordinate, PlanVersion
from .integrations import GraphHopperClient, OneMapClient


def decode_polyline(encoded: str, precision: int = 5) -> tuple[Coordinate, ...]:
    coordinates = []
    index = 0
    latitude = 0
    longitude = 0
    factor = 10**precision
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result = 0
            shift = 0
            while True:
                value = ord(encoded[index]) - 63
                index += 1
                result |= (value & 0x1F) << shift
                shift += 5
                if value < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        coordinates.append(Coordinate(lat=latitude / factor, lon=longitude / factor))
    return tuple(coordinates)


async def road_distance_matrix(
    points: tuple[tuple[str, Coordinate], ...],
    onemap: OneMapClient,
    *,
    concurrency: int = 4,
) -> dict[tuple[str, str], float]:
    """Real driving distance (km) between every ordered pair in ``points``.

    ``points`` is a list of ``(id, coordinate)`` covering one vehicle's cluster
    (its depot plus its assigned stops). Returns a ``{(from_id, to_id): km}``
    lookup used by the planner to sequence stops on real road distance instead
    of straight-line distance. Pairs whose OneMap lookup fails are simply
    omitted, so the caller transparently falls back to haversine for them —
    planning never breaks because a leg could not be routed.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def leg(from_id: str, to_id: str, start: Coordinate, end: Coordinate):
        try:
            async with semaphore:
                response = await onemap.route((start.lat, start.lon), (end.lat, end.lon))
            distance = float(response.get("route_summary", {}).get("total_distance", 0)) / 1000
            return (from_id, to_id), distance if distance > 0 else None
        except (httpx.HTTPError, KeyError, RuntimeError, TypeError, ValueError):
            return (from_id, to_id), None

    tasks = [
        leg(a_id, b_id, a_pt, b_pt)
        for a_id, a_pt in points
        for b_id, b_pt in points
        if a_id != b_id
    ]
    results = await asyncio.gather(*tasks)
    return {pair: distance for pair, distance in results if distance is not None}


async def enrich_plan_geometry(
    plan: PlanVersion,
    depot: Coordinate,
    onemap: OneMapClient,
    depot_by_vehicle: dict[str, Coordinate] | None = None,
) -> PlanVersion:
    """Attach road geometry per route.

    ``depot`` is the fallback origin/terminus. ``depot_by_vehicle`` supplies the
    per-vehicle depot coordinate for multi-depot plans; routes without an entry
    fall back to ``depot``.
    """
    depot_by_vehicle = depot_by_vehicle or {}
    semaphore = asyncio.Semaphore(3)

    async def route_leg(
        start: Coordinate, end: Coordinate
    ) -> tuple[tuple[Coordinate, ...] | None, float]:
        try:
            async with semaphore:
                response = await onemap.route((start.lat, start.lon), (end.lat, end.lon))
            geometry = decode_polyline(str(response["route_geometry"]))
            distance = float(response.get("route_summary", {}).get("total_distance", 0)) / 1000
            return geometry or None, distance
        except (httpx.HTTPError, KeyError, RuntimeError, TypeError, ValueError):
            return None, 0.0

    route_inputs = []
    for route in plan.routes:
        origin = depot_by_vehicle.get(route.vehicle_id, depot)
        points = (origin, *(stop.location for stop in route.stops), origin)
        route_inputs.append(tuple(zip(points, points[1:], strict=False)))
    leg_results = await asyncio.gather(
        *(route_leg(start, end) for legs in route_inputs for start, end in legs)
    )
    cursor = 0
    updated_routes = []
    for route, legs in zip(plan.routes, route_inputs, strict=True):
        route_results = leg_results[cursor : cursor + len(legs)]
        cursor += len(legs)
        geometry = []
        road_distance = 0.0
        routing_complete = True
        for leg_geometry, leg_distance in route_results:
            if leg_geometry is None:
                routing_complete = False
                continue
            geometry.extend(leg_geometry if not geometry else leg_geometry[1:])
            road_distance += leg_distance
        updated_routes.append(
            route.model_copy(
                update={
                    "geometry": tuple(geometry) if routing_complete else (),
                    "distance_km": (
                        round(road_distance, 2)
                        if routing_complete and road_distance
                        else route.distance_km
                    ),
                }
            )
        )
    return plan.model_copy(
        update={
            "routes": tuple(updated_routes),
            "objective_cost": round(sum(route.distance_km for route in updated_routes), 2),
        }
    )


def _leg_crosses_road_path(
    start: Coordinate,
    end: Coordinate,
    road_path: tuple[Coordinate, ...],
    buffer_m: float = 200.0,
) -> bool:
    """Whether the straight leg start->end comes within ``buffer_m`` metres of
    any point on ``road_path``.

    Used to decide whether a reroute geometry leg needs a detour waypoint to
    steer it away from the closed road.  A simple proximity check (minimum
    distance from leg midpoint to any road_path vertex) is enough here —
    we don't need an exact intersection test; we just need to know "does this
    leg look like it travels near the closed road".
    """
    # Check the leg midpoint and both endpoints against every road_path vertex.
    mid = Coordinate(lat=(start.lat + end.lat) / 2, lon=(start.lon + end.lon) / 2)
    m_per_deg = 111_320.0
    threshold_deg = buffer_m / m_per_deg
    for probe in (start, mid, end):
        for rp in road_path:
            dlat = probe.lat - rp.lat
            dlon = probe.lon - rp.lon
            if math.hypot(dlat, dlon) < threshold_deg:
                return True
    return False


def _detour_waypoint(
    road_path: tuple[Coordinate, ...],
    offset_m: float = 300.0,
) -> Coordinate:
    """A point perpendicular to the road_path midpoint, offset by ``offset_m``
    metres.  Used as an intermediate waypoint to force OneMap's routing to
    detour around the closed road segment.

    The perpendicular direction is chosen arbitrarily (left side of the road
    in travel direction).  Because Singapore's road network is dense, a 300 m
    offset almost always produces an alternative route.
    """
    # Use the midpoint of the road_path as the reference.
    mid_idx = len(road_path) // 2
    if mid_idx == 0:
        mid_idx = 0
    ref = road_path[mid_idx]
    # Direction of the road at that point.
    if mid_idx + 1 < len(road_path):
        fwd = road_path[mid_idx + 1]
    elif mid_idx > 0:
        fwd = road_path[mid_idx]
        ref = road_path[mid_idx - 1]
    else:
        # Single-point path — return a fixed small offset.
        return Coordinate(lat=ref.lat + offset_m / 111_320.0, lon=ref.lon)

    dx = fwd.lon - ref.lon
    dy = fwd.lat - ref.lat
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return Coordinate(lat=ref.lat + offset_m / 111_320.0, lon=ref.lon)

    # Perpendicular (left of travel direction): (-dy, dx) normalised.
    mid_lat = (ref.lat + fwd.lat) / 2
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    perp_lat = -dx / length * (offset_m / m_per_deg_lat)
    perp_lon = dy / length * (offset_m / m_per_deg_lon)
    candidate = Coordinate(
        lat=ref.lat + perp_lat,
        lon=ref.lon + perp_lon,
    )
    # Clamp to Singapore bounding box so the waypoint stays on-map.
    return Coordinate(
        lat=max(1.15, min(1.5, candidate.lat)),
        lon=max(103.55, min(104.1, candidate.lon)),
    )


async def enrich_plan_geometry_avoiding(
    plan: PlanVersion,
    depot: Coordinate,
    onemap: OneMapClient,
    road_path: tuple[Coordinate, ...],
    *,
    depot_by_vehicle: dict[str, Coordinate] | None = None,
) -> PlanVersion:
    """Like ``enrich_plan_geometry`` but routes legs that come close to
    ``road_path`` via a detour waypoint, so the displayed geometry visually
    avoids the closed road segment.

    For each leg (origin→destination) of each route:
    - If the straight leg comes within ~80 m of the closed road_path, split
      it into two OneMap calls via a perpendicular detour waypoint
      (origin→waypoint→destination) and stitch the results.
    - Otherwise, route it directly as usual.

    Best-effort: if either half of a split leg fails, falls back to a direct
    routing attempt.  If that also fails, the leg is omitted (same as
    ``enrich_plan_geometry``).
    """
    depot_by_vehicle = depot_by_vehicle or {}
    semaphore = asyncio.Semaphore(3)
    waypoint = _detour_waypoint(road_path) if len(road_path) >= 2 else None

    async def route_leg_direct(
        start: Coordinate, end: Coordinate
    ) -> tuple[tuple[Coordinate, ...] | None, float]:
        try:
            async with semaphore:
                response = await onemap.route((start.lat, start.lon), (end.lat, end.lon))
            geometry = decode_polyline(str(response["route_geometry"]))
            distance = float(response.get("route_summary", {}).get("total_distance", 0)) / 1000
            return geometry or None, distance
        except (httpx.HTTPError, KeyError, RuntimeError, TypeError, ValueError):
            return None, 0.0

    async def route_leg(
        start: Coordinate, end: Coordinate
    ) -> tuple[tuple[Coordinate, ...] | None, float]:
        """Route one leg, detouring via the waypoint if the leg is near the
        closed road."""
        if waypoint is not None and _leg_crosses_road_path(start, end, road_path):
            # Two-hop routing: start→waypoint then waypoint→end.
            geo_a, dist_a = await route_leg_direct(start, waypoint)
            geo_b, dist_b = await route_leg_direct(waypoint, end)
            if geo_a is not None and geo_b is not None:
                stitched = geo_a + geo_b[1:]  # drop duplicate waypoint vertex
                return stitched, dist_a + dist_b
            # One or both hops failed — fall back to direct.
        return await route_leg_direct(start, end)

    route_inputs = []
    for route in plan.routes:
        origin = depot_by_vehicle.get(route.vehicle_id, depot)
        points = (origin, *(stop.location for stop in route.stops), origin)
        route_inputs.append(tuple(zip(points, points[1:], strict=False)))

    leg_results = await asyncio.gather(
        *(route_leg(start, end) for legs in route_inputs for start, end in legs)
    )
    cursor = 0
    updated_routes = []
    for route, legs in zip(plan.routes, route_inputs, strict=True):
        route_results = leg_results[cursor : cursor + len(legs)]
        cursor += len(legs)
        geometry: list[Coordinate] = []
        road_distance = 0.0
        routing_complete = True
        for leg_geometry, leg_distance in route_results:
            if leg_geometry is None:
                routing_complete = False
                continue
            geometry.extend(leg_geometry if not geometry else leg_geometry[1:])
            road_distance += leg_distance
        updated_routes.append(
            route.model_copy(
                update={
                    "geometry": tuple(geometry) if routing_complete else (),
                    "distance_km": (
                        round(road_distance, 2)
                        if routing_complete and road_distance
                        else route.distance_km
                    ),
                }
            )
        )
    return plan.model_copy(
        update={
            "routes": tuple(updated_routes),
            "objective_cost": round(sum(route.distance_km for route in updated_routes), 2),
        }
    )


async def enrich_plan_geometry_graphhopper(
    plan: PlanVersion,
    depot: Coordinate,
    onemap: OneMapClient,
    graphhopper: GraphHopperClient,
    avoid_polygon: tuple[Coordinate, ...],
    *,
    road_path: tuple[Coordinate, ...] | None = None,
    depot_by_vehicle: dict[str, Coordinate] | None = None,
) -> PlanVersion:
    """Enrich route geometry using GraphHopper for legs near the closure,
    OneMap for all other legs.

    Every leg is routed through GraphHopper with ``avoid_polygon`` as a hard
    ``custom_model`` block. If GraphHopper fails for a leg, the request falls
    back to OneMap. ``road_path`` is retained for call-site compatibility and
    disruption evidence.
    """

    depot_by_vehicle = depot_by_vehicle or {}
    semaphore = asyncio.Semaphore(3)
    async def _onemap_leg(
        start: Coordinate, end: Coordinate
    ) -> tuple[tuple[Coordinate, ...] | None, float]:
        try:
            async with semaphore:
                response = await onemap.route((start.lat, start.lon), (end.lat, end.lon))
            geometry = decode_polyline(str(response["route_geometry"]))
            distance = float(response.get("route_summary", {}).get("total_distance", 0)) / 1000
            return geometry or None, distance
        except (httpx.HTTPError, KeyError, RuntimeError, TypeError, ValueError):
            return None, 0.0

    async def _gh_leg(
        start: Coordinate, end: Coordinate
    ) -> tuple[tuple[Coordinate, ...] | None, float]:
        """Route via GraphHopper avoiding the closure polygon."""
        try:
            async with semaphore:
                geometry = await graphhopper.route_avoiding(
                    (start.lat, start.lon), (end.lat, end.lon), avoid_polygon
                )
            # Compute haversine distance over the returned geometry.
            distance = sum(
                math.sqrt(
                    ((geometry[i + 1].lat - geometry[i].lat) * 111_320.0) ** 2
                    + ((geometry[i + 1].lon - geometry[i].lon)
                       * 111_320.0 * math.cos(math.radians(geometry[i].lat))) ** 2
                )
                / 1000
                for i in range(len(geometry) - 1)
            )
            return geometry, distance
        except (RuntimeError, Exception):  # noqa: BLE001
            # GraphHopper failed — fall back to OneMap for this leg.
            return await _onemap_leg(start, end)

    route_inputs = []
    for route in plan.routes:
        origin = depot_by_vehicle.get(route.vehicle_id, depot)
        points = (origin, *(stop.location for stop in route.stops), origin)
        route_inputs.append(tuple(zip(points, points[1:], strict=False)))

    # Route ALL legs through GH when a closure is active — the straight-line
    # proximity check is unreliable because real routes curve through roads,
    # so a leg whose straight segment misses the closure may still drive through
    # it on the actual road network. GH with the polygon block handles every leg
    # correctly; non-crossing legs are unaffected since GH just routes normally.
    tasks = []
    for legs in route_inputs:
        for start, end in legs:
            tasks.append(_gh_leg(start, end))

    leg_results = await asyncio.gather(*tasks)

    cursor = 0
    updated_routes = []
    for route, legs in zip(plan.routes, route_inputs, strict=True):
        route_results = leg_results[cursor : cursor + len(legs)]
        cursor += len(legs)
        geometry: list[Coordinate] = []
        road_distance = 0.0
        routing_complete = True
        for leg_geometry, leg_distance in route_results:
            if leg_geometry is None:
                routing_complete = False
                continue
            geometry.extend(leg_geometry if not geometry else leg_geometry[1:])
            road_distance += leg_distance
        updated_routes.append(
            route.model_copy(
                update={
                    "geometry": tuple(geometry) if routing_complete else (),
                    "distance_km": (
                        round(road_distance, 2)
                        if routing_complete and road_distance
                        else route.distance_km
                    ),
                }
            )
        )
    return plan.model_copy(
        update={
            "routes": tuple(updated_routes),
            "objective_cost": round(sum(route.distance_km for route in updated_routes), 2),
        }
    )
