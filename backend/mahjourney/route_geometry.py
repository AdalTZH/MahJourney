from __future__ import annotations

import asyncio

import httpx

from .domain import Coordinate, PlanVersion
from .integrations import OneMapClient


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


async def enrich_plan_geometry(
    plan: PlanVersion, depot: Coordinate, onemap: OneMapClient
) -> PlanVersion:
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
        points = (depot, *(stop.location for stop in route.stops), depot)
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
