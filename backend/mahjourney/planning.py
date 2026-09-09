from __future__ import annotations

import math
from collections import defaultdict
from uuid import uuid4

from .domain import Coordinate, Order, PlanDelta, PlanVersion, RouteStop, Vehicle, VehicleRoute


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = math.radians(b.lat - a.lat)
    dlon = math.radians(b.lon - a.lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def travel_minutes(a: Coordinate, b: Coordinate, speed_kph: float = 28.0) -> int:
    return max(1, math.ceil(haversine_km(a, b) / speed_kph * 60))


def _nearest_neighbor(vehicle: Vehicle, orders: list[Order]) -> list[Order]:
    remaining = list(orders)
    current = vehicle.start
    route: list[Order] = []
    while remaining:
        chosen = min(
            remaining,
            key=lambda order: (
                max(0, 480 + travel_minutes(current, order.location) - order.window_end_minute),
                haversine_km(current, order.location),
                order.order_id,
            ),
        )
        route.append(chosen)
        remaining.remove(chosen)
        current = chosen.location
    return route


def _ortools_order(
    vehicle: Vehicle, orders: list[Order], speed_kph_by_stop: dict[str, float] | None = None
) -> list[Order]:
    """Solve one capacity/time-window route; retain a deterministic fallback."""
    if not orders:
        return []
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2

        locations = [vehicle.start, *(order.location for order in orders)]
        manager = pywrapcp.RoutingIndexManager(len(locations), 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def transit(from_index: int, to_index: int) -> int:
            source = locations[manager.IndexToNode(from_index)]
            target = locations[manager.IndexToNode(to_index)]
            target_node = manager.IndexToNode(to_index)
            speed = (
                speed_kph_by_stop.get(orders[target_node - 1].order_id, 28.0)
                if speed_kph_by_stop and target_node
                else 28.0
            )
            return travel_minutes(source, target, speed) + (5 if target_node else 0)

        transit_index = routing.RegisterTransitCallback(transit)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
        routing.AddDimension(transit_index, 90, 600, False, "Time")
        time_dimension = routing.GetDimensionOrDie("Time")
        time_dimension.CumulVar(routing.Start(0)).SetRange(480, 480)
        for node, order in enumerate(orders, start=1):
            index = manager.NodeToIndex(node)
            time_dimension.CumulVar(index).SetRange(
                order.window_start_minute, order.window_end_minute
            )

        def demand(from_index: int) -> int:
            node = manager.IndexToNode(from_index)
            return 0 if node == 0 else orders[node - 1].demand

        demand_index = routing.RegisterUnaryTransitCallback(demand)
        routing.AddDimensionWithVehicleCapacity(
            demand_index, 0, [vehicle.capacity], True, "Capacity"
        )
        search = pywrapcp.DefaultRoutingSearchParameters()
        search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search.time_limit.FromMilliseconds(250)
        solution = routing.SolveWithParameters(search)
        if solution is None:
            return _nearest_neighbor(vehicle, orders)
        ordered: list[Order] = []
        index = routing.Start(0)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node:
                ordered.append(orders[node - 1])
            index = solution.Value(routing.NextVar(index))
        return ordered
    except (ImportError, RuntimeError, ValueError):
        return _nearest_neighbor(vehicle, orders)


def _build_route(
    vehicle: Vehicle, orders: list[Order], speed_kph_by_stop: dict[str, float] | None = None
) -> VehicleRoute:
    minute = 480
    current = vehicle.start
    distance = 0.0
    stops: list[RouteStop] = []
    for sequence, order in enumerate(orders, start=1):
        distance += haversine_km(current, order.location)
        speed = speed_kph_by_stop.get(order.order_id, 28.0) if speed_kph_by_stop else 28.0
        minute += travel_minutes(current, order.location, speed)
        minute = max(minute, order.window_start_minute)
        departure = minute + math.ceil(order.service_seconds / 60)
        stops.append(
            RouteStop(
                stop_id=order.order_id,
                sequence=sequence,
                location=order.location,
                eta_minute=minute,
                departure_minute=departure,
                demand=order.demand,
            )
        )
        minute = departure
        current = order.location
    distance += haversine_km(current, vehicle.start)
    minute += travel_minutes(current, vehicle.start)
    return VehicleRoute(
        vehicle_id=vehicle.vehicle_id,
        driver_id=vehicle.driver_id,
        stops=tuple(stops),
        distance_km=round(distance, 2),
        duration_minutes=minute - 480,
    )


def validate_plan(
    plan: PlanVersion, vehicles: tuple[Vehicle, ...], orders: tuple[Order, ...]
) -> tuple[str, ...]:
    violations: list[str] = []
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    order_by_id = {order.order_id: order for order in orders}
    seen: set[str] = set()
    for route in plan.routes:
        vehicle = vehicle_by_id.get(route.vehicle_id)
        if vehicle is None:
            violations.append(f"unknown vehicle {route.vehicle_id}")
            continue
        load = sum(stop.demand for stop in route.stops)
        if load > vehicle.capacity:
            violations.append(f"{route.vehicle_id} capacity {load}>{vehicle.capacity}")
        for stop in route.stops:
            if stop.stop_id in seen:
                violations.append(f"duplicate stop {stop.stop_id}")
            seen.add(stop.stop_id)
            order = order_by_id.get(stop.stop_id)
            if order is None:
                violations.append(f"unknown stop {stop.stop_id}")
            elif not order.window_start_minute <= stop.eta_minute <= order.window_end_minute:
                violations.append(f"{stop.stop_id} outside time window")
    missing = set(order_by_id) - seen
    violations.extend(f"unassigned stop {stop_id}" for stop_id in sorted(missing))
    return tuple(violations)


def build_plan(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    *,
    plan_id: str | None = None,
    version: int = 1,
    source_data_version: str = "fixture-v1",
    speed_kph_by_stop: dict[str, float] | None = None,
) -> PlanVersion:
    # A deterministic geographic sweep creates capacity-feasible clusters; nearest-neighbor
    # ordering inside each cluster is the offline fallback when OR-Tools is unavailable.
    sorted_orders = sorted(
        orders,
        key=lambda order: math.atan2(
            order.location.lat - vehicles[0].start.lat, order.location.lon - vehicles[0].start.lon
        ),
    )
    buckets: dict[str, list[Order]] = defaultdict(list)
    stops_per_vehicle = math.ceil(len(sorted_orders) / len(vehicles))
    for index, order in enumerate(sorted_orders):
        vehicle_index = min(index // stops_per_vehicle, len(vehicles) - 1)
        buckets[vehicles[vehicle_index].vehicle_id].append(order)
    routes = tuple(
        _build_route(
            vehicle,
            _ortools_order(vehicle, buckets[vehicle.vehicle_id], speed_kph_by_stop),
            speed_kph_by_stop,
        )
        for vehicle in vehicles
    )
    provisional = PlanVersion(
        plan_id=plan_id or str(uuid4()),
        version=version,
        status="CANDIDATE",
        source_data_version=source_data_version,
        routes=routes,
        objective_cost=round(sum(route.distance_km for route in routes), 2),
    )
    violations = validate_plan(provisional, vehicles, orders)
    return provisional.model_copy(
        update={
            "status": "VALIDATED" if not violations else "CANDIDATE",
            "hard_violations": violations,
        }
    )


def greedy_baseline(vehicles: tuple[Vehicle, ...], orders: tuple[Order, ...]) -> PlanVersion:
    buckets: dict[str, list[Order]] = defaultdict(list)
    for index, order in enumerate(orders):
        vehicle_index = min(index // math.ceil(len(orders) / len(vehicles)), len(vehicles) - 1)
        buckets[vehicles[vehicle_index].vehicle_id].append(order)
    routes = tuple(_build_route(vehicle, buckets[vehicle.vehicle_id]) for vehicle in vehicles)
    return PlanVersion(
        plan_id="greedy-baseline",
        version=1,
        status="VALIDATED",
        source_data_version="fixture-v1",
        routes=routes,
        objective_cost=round(sum(route.distance_km for route in routes), 2),
    )


def plan_duration_under_speeds(
    plan: PlanVersion,
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    speed_kph_by_stop: dict[str, float],
) -> int:
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    order_by_id = {order.order_id: order for order in orders}
    total = 0
    for route in plan.routes:
        vehicle = vehicle_by_id[route.vehicle_id]
        current = vehicle.start
        for stop in route.stops:
            order = order_by_id[stop.stop_id]
            total += travel_minutes(
                current, stop.location, speed_kph_by_stop.get(stop.stop_id, 28.0)
            )
            total += math.ceil(order.service_seconds / 60)
            current = stop.location
        total += travel_minutes(current, vehicle.start)
    return total


def plan_delta(before: PlanVersion, after: PlanVersion) -> PlanDelta:
    before_assignment = {
        stop.stop_id: route.vehicle_id for route in before.routes for stop in route.stops
    }
    after_assignment = {
        stop.stop_id: route.vehicle_id for route in after.routes for stop in route.stops
    }
    moved = tuple(
        sorted(
            stop
            for stop, vehicle in after_assignment.items()
            if before_assignment.get(stop) != vehicle
        )
    )
    changed = tuple(
        sorted(
            {after_assignment[stop] for stop in moved}
            | {before_assignment.get(stop, "") for stop in moved} - {""}
        )
    )
    return PlanDelta(
        from_version=before.version,
        to_version=after.version,
        changed_vehicles=changed,
        moved_stops=moved,
        cost_change=round(after.objective_cost - before.objective_cost, 2),
    )
