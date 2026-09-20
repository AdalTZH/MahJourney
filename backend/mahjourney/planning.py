from __future__ import annotations

import math
from collections import defaultdict
from uuid import uuid4

from .domain import (
    Coordinate,
    Depot,
    Order,
    PlanDelta,
    PlanVersion,
    RouteStop,
    Vehicle,
    VehicleRoute,
)

# Realistic capacity dimensions are scaled to integers because OR-Tools capacity
# dimensions operate on integers. Grams and litres keep enough precision.
_WEIGHT_SCALE = 1000  # kg -> grams
_VOLUME_SCALE = 1000  # m3 -> litres
DEFAULT_MAX_STOPS_PER_VEHICLE = 25


def order_service_seconds(order: Order) -> int:
    """Service time grows with parcel count without letting quantity dominate."""
    return order.service_seconds + max(0, order.quantity - 1) * 30


def effective_window(
    order: Order, vehicle: Vehicle, *, enforce_delivery_windows: bool = True
) -> tuple[int, int]:
    """The delivery window planning should actually honor for this order.

    When ``enforce_delivery_windows`` is true (the default), this is just the
    order's own ``window_start_minute``/``window_end_minute``. When false, the
    order can be served any time within the vehicle's working hours instead —
    early or late are both fine — so callers that plan or validate against a
    window should go through this helper rather than reading the order's
    fields directly.
    """
    if enforce_delivery_windows:
        return order.window_start_minute, order.window_end_minute
    return vehicle.working_start_minute, vehicle.working_end_minute


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = math.radians(b.lat - a.lat)
    dlon = math.radians(b.lon - a.lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def travel_minutes(a: Coordinate, b: Coordinate, speed_kph: float = 28.0) -> int:
    return max(1, math.ceil(haversine_km(a, b) / speed_kph * 60))


# A vehicle's depot node id in a road-distance matrix is derived from its start
# coordinate so that a single flat matrix can cover multiple depots without the
# depot legs of different vehicles colliding on one shared key.
def depot_node_id(coord: Coordinate) -> str:
    return f"depot:{coord.lat:.6f},{coord.lon:.6f}"


def leg_distance_km(
    from_id: str,
    to_id: str,
    a: Coordinate,
    b: Coordinate,
    road_distance_km: dict[tuple[str, str], float] | None,
) -> float:
    """Road distance for a leg when available, else straight-line haversine.

    ``road_distance_km`` maps ``(from_id, to_id)`` to real driving distance in
    km (built from OneMap). Any pair missing from it — because that lookup
    failed or road distances are disabled — transparently falls back to the
    haversine estimate, so planning never depends on OneMap being reachable.
    """
    if road_distance_km is not None:
        km = road_distance_km.get((from_id, to_id))
        if km is not None:
            return km
    return haversine_km(a, b)


def leg_travel_minutes(
    from_id: str,
    to_id: str,
    a: Coordinate,
    b: Coordinate,
    speed_kph: float,
    road_distance_km: dict[tuple[str, str], float] | None,
) -> int:
    km = leg_distance_km(from_id, to_id, a, b, road_distance_km)
    return max(1, math.ceil(km / speed_kph * 60))


def _fits(vehicle: Vehicle, load: list[Order], candidate: Order, max_stops: int) -> bool:
    """Whether adding ``candidate`` keeps the load within every hard capacity."""
    if len(load) + 1 > max_stops:
        return False
    weight = sum(order.weight_kg for order in load) + candidate.weight_kg
    volume = sum(order.volume_m3 for order in load) + candidate.volume_m3
    return weight <= vehicle.capacity_weight_kg and volume <= vehicle.capacity_volume_m3


def _nearest_neighbor(
    vehicle: Vehicle,
    orders: list[Order],
    max_stops: int = DEFAULT_MAX_STOPS_PER_VEHICLE,
    road_distance_km: dict[tuple[str, str], float] | None = None,
    enforce_delivery_windows: bool = True,
) -> list[Order]:
    remaining = list(orders)
    current = vehicle.start
    current_id = depot_node_id(vehicle.start)
    start_minute = vehicle.working_start_minute
    route: list[Order] = []
    while remaining:
        feasible = [order for order in remaining if _fits(vehicle, route, order, max_stops)]
        if not feasible:
            break
        chosen = min(
            feasible,
            key=lambda order: (
                max(
                    0,
                    start_minute
                    + leg_travel_minutes(
                        current_id, order.order_id, current, order.location, 28.0, road_distance_km
                    )
                    - effective_window(
                        order, vehicle, enforce_delivery_windows=enforce_delivery_windows
                    )[1],
                ),
                leg_distance_km(
                    current_id, order.order_id, current, order.location, road_distance_km
                ),
                order.order_id,
            ),
        )
        route.append(chosen)
        remaining.remove(chosen)
        current_id = chosen.order_id
        current = chosen.location
    return route


def _ortools_order(
    vehicle: Vehicle,
    orders: list[Order],
    speed_kph_by_stop: dict[str, float] | None = None,
    max_stops: int = DEFAULT_MAX_STOPS_PER_VEHICLE,
    allow_drops: bool = False,
    road_distance_km: dict[tuple[str, str], float] | None = None,
    enforce_delivery_windows: bool = True,
) -> list[Order]:
    """Solve one route under weight/volume/time/working-hours/max-stops constraints.

    When ``road_distance_km`` is supplied, leg costs/times use real OneMap road
    distance so the visit order is optimized on driving distance; missing pairs
    fall back to haversine. Retains a deterministic capacity-aware
    nearest-neighbor fallback so the system still plans when OR-Tools is
    unavailable or fails to converge. When ``enforce_delivery_windows`` is
    false, every order's effective window is the vehicle's own working
    window, so orders can be served any time the vehicle/driver is available.
    """
    if not orders:
        return []
    start_minute = vehicle.working_start_minute
    end_minute = max(vehicle.working_start_minute + 1, vehicle.working_end_minute)
    horizon = max(1, end_minute - start_minute)
    node_ids = [depot_node_id(vehicle.start), *(order.order_id for order in orders)]
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2

        locations = [vehicle.start, *(order.location for order in orders)]
        manager = pywrapcp.RoutingIndexManager(len(locations), 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        def transit(from_index: int, to_index: int) -> int:
            source_node = manager.IndexToNode(from_index)
            target_node = manager.IndexToNode(to_index)
            source = locations[source_node]
            target = locations[target_node]
            speed = (
                speed_kph_by_stop.get(orders[target_node - 1].order_id, 28.0)
                if speed_kph_by_stop and target_node
                else 28.0
            )
            service = (
                math.ceil(order_service_seconds(orders[target_node - 1]) / 60)
                if target_node
                else 0
            )
            travel = leg_travel_minutes(
                node_ids[source_node], node_ids[target_node], source, target, speed,
                road_distance_km,
            )
            return travel + service

        transit_index = routing.RegisterTransitCallback(transit)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
        # Time dimension spans the whole shift; the depot start is pinned to the
        # working-hours start and every served node must respect its window.
        routing.AddDimension(transit_index, horizon, end_minute, False, "Time")
        time_dimension = routing.GetDimensionOrDie("Time")
        time_dimension.CumulVar(routing.Start(0)).SetRange(start_minute, start_minute)
        # A drop penalty larger than any achievable route cost guarantees that
        # OR-Tools only sheds an order when it is genuinely infeasible.
        drop_penalty = end_minute * len(orders) + 1_000_000
        for node, order in enumerate(orders, start=1):
            index = manager.NodeToIndex(node)
            order_window_start, order_window_end = effective_window(
                order, vehicle, enforce_delivery_windows=enforce_delivery_windows
            )
            window_start = max(order_window_start, start_minute)
            window_end = min(order_window_end, end_minute)
            if window_start > window_end:
                # Cannot be served inside this vehicle's shift.
                if allow_drops:
                    # Force-drop: make the node mandatory-to-skip by giving it an
                    # empty feasible window plus a disjunction so the solver
                    # excludes it instead of serving it past working hours.
                    routing.AddDisjunction([index], 0)
                    continue
                # Legacy single-origin path: keep the order's own window so the
                # historical fixture behavior (no drops) is preserved.
                window_start, window_end = order_window_start, order_window_end
            time_dimension.CumulVar(index).SetRange(window_start, window_end)
            if allow_drops:
                routing.AddDisjunction([index], drop_penalty)

        def weight_demand(from_index: int) -> int:
            node = manager.IndexToNode(from_index)
            return 0 if node == 0 else round(orders[node - 1].weight_kg * _WEIGHT_SCALE)

        def volume_demand(from_index: int) -> int:
            node = manager.IndexToNode(from_index)
            return 0 if node == 0 else round(orders[node - 1].volume_m3 * _VOLUME_SCALE)

        def stop_demand(from_index: int) -> int:
            return 0 if manager.IndexToNode(from_index) == 0 else 1

        weight_index = routing.RegisterUnaryTransitCallback(weight_demand)
        routing.AddDimensionWithVehicleCapacity(
            weight_index, 0, [round(vehicle.capacity_weight_kg * _WEIGHT_SCALE)], True, "Weight"
        )
        volume_index = routing.RegisterUnaryTransitCallback(volume_demand)
        routing.AddDimensionWithVehicleCapacity(
            volume_index, 0, [round(vehicle.capacity_volume_m3 * _VOLUME_SCALE)], True, "Volume"
        )
        stop_index = routing.RegisterUnaryTransitCallback(stop_demand)
        routing.AddDimensionWithVehicleCapacity(stop_index, 0, [max_stops], True, "Stops")

        search = pywrapcp.DefaultRoutingSearchParameters()
        search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search.time_limit.FromMilliseconds(250)
        solution = routing.SolveWithParameters(search)
        if solution is None:
            return _nearest_neighbor(
                vehicle, orders, max_stops, road_distance_km, enforce_delivery_windows
            )
        ordered: list[Order] = []
        index = routing.Start(0)
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node:
                ordered.append(orders[node - 1])
            index = solution.Value(routing.NextVar(index))
        return ordered
    except (ImportError, RuntimeError, ValueError):
        return _nearest_neighbor(
            vehicle, orders, max_stops, road_distance_km, enforce_delivery_windows
        )


def _build_route(
    vehicle: Vehicle,
    orders: list[Order],
    speed_kph_by_stop: dict[str, float] | None = None,
    road_distance_km: dict[tuple[str, str], float] | None = None,
    enforce_delivery_windows: bool = True,
) -> VehicleRoute:
    minute = vehicle.working_start_minute
    current = vehicle.start
    current_id = depot_node_id(vehicle.start)
    distance = 0.0
    # Moment the vehicle actually rolls out of the depot toward its first stop.
    # A vehicle whose first delivery window opens later waits at the depot rather
    # than departing early, so leading idle time is excluded from duration.
    depart_depot_minute: int | None = None
    stops: list[RouteStop] = []
    for sequence, order in enumerate(orders, start=1):
        distance += leg_distance_km(
            current_id, order.order_id, current, order.location, road_distance_km
        )
        speed = speed_kph_by_stop.get(order.order_id, 28.0) if speed_kph_by_stop else 28.0
        leg = leg_travel_minutes(
            current_id, order.order_id, current, order.location, speed, road_distance_km
        )
        arrival = minute + leg
        window_start, _ = effective_window(
            order, vehicle, enforce_delivery_windows=enforce_delivery_windows
        )
        service_start = max(arrival, window_start)
        if depart_depot_minute is None:
            # Leave the depot just in time to reach the first stop as it is
            # served, so pre-window waiting happens off the clock (at the depot).
            depart_depot_minute = service_start - leg
        minute = service_start
        departure = minute + math.ceil(order_service_seconds(order) / 60)
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
        current_id = order.order_id
    return_id = depot_node_id(vehicle.start)
    distance += leg_distance_km(current_id, return_id, current, vehicle.start, road_distance_km)
    minute += leg_travel_minutes(
        current_id, return_id, current, vehicle.start, 28.0, road_distance_km
    )
    # Active route time: from rolling out of the depot to returning, excluding
    # the idle wait before the first delivery window opens. Empty routes are 0.
    start_reference = depart_depot_minute if depart_depot_minute is not None else minute
    return VehicleRoute(
        vehicle_id=vehicle.vehicle_id,
        driver_id=vehicle.driver_id,
        stops=tuple(stops),
        distance_km=round(distance, 2),
        duration_minutes=minute - start_reference,
    )


def reapply_route_timing(
    plan: PlanVersion,
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    speed_kph_by_stop: dict[str, float] | None,
    *,
    version: int | None = None,
    source_data_version: str | None = None,
    enforce_delivery_windows: bool = True,
) -> PlanVersion:
    """Recompute a plan's travel times, ETAs, and durations under new speeds.

    Keeps the existing stop assignment and sequence per vehicle unchanged — this
    is not a replan. It answers "how much slower does the current plan become
    under these conditions", which is what a live disruption should update:
    an active road closure or heavy rain should change how long the current
    plan takes, not silently reassign who serves what.
    """
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    order_by_id = {order.order_id: order for order in orders}
    routes: list[VehicleRoute] = []
    for route in plan.routes:
        vehicle = vehicle_by_id.get(route.vehicle_id)
        if vehicle is None:
            routes.append(route)
            continue
        sequence_orders = [
            order_by_id[stop.stop_id] for stop in route.stops if stop.stop_id in order_by_id
        ]
        routes.append(
            _build_route(
                vehicle,
                sequence_orders,
                speed_kph_by_stop,
                enforce_delivery_windows=enforce_delivery_windows,
            )
        )
    updated = plan.model_copy(
        update={
            "routes": tuple(routes),
            "objective_cost": round(sum(route.distance_km for route in routes), 2),
            **({"version": version} if version is not None else {}),
            **(
                {"source_data_version": source_data_version}
                if source_data_version is not None
                else {}
            ),
        }
    )
    violations = validate_plan(
        updated, vehicles, orders, enforce_delivery_windows=enforce_delivery_windows
    )
    return updated.model_copy(
        update={
            "status": "VALIDATED" if not violations else "CANDIDATE",
            "hard_violations": violations,
        }
    )


def validate_plan(
    plan: PlanVersion,
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    max_stops: int = DEFAULT_MAX_STOPS_PER_VEHICLE,
    enforce_delivery_windows: bool = True,
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
        route_orders = [
            order_by_id[stop.stop_id] for stop in route.stops if stop.stop_id in order_by_id
        ]
        weight = sum(order.weight_kg for order in route_orders)
        volume = sum(order.volume_m3 for order in route_orders)
        if weight > vehicle.capacity_weight_kg + 1e-6:
            violations.append(
                f"{route.vehicle_id} weight {weight:.1f}>{vehicle.capacity_weight_kg:.1f}kg"
            )
        if volume > vehicle.capacity_volume_m3 + 1e-6:
            violations.append(
                f"{route.vehicle_id} volume {volume:.3f}>{vehicle.capacity_volume_m3:.3f}m3"
            )
        if len(route.stops) > max_stops:
            violations.append(f"{route.vehicle_id} stops {len(route.stops)}>{max_stops}")
        for stop in route.stops:
            if stop.stop_id in seen:
                violations.append(f"duplicate stop {stop.stop_id}")
            seen.add(stop.stop_id)
            order = order_by_id.get(stop.stop_id)
            if order is None:
                violations.append(f"unknown stop {stop.stop_id}")
                continue
            window_start, window_end = effective_window(
                order, vehicle, enforce_delivery_windows=enforce_delivery_windows
            )
            if not window_start <= stop.eta_minute <= window_end:
                violations.append(f"{stop.stop_id} outside time window")
            if stop.eta_minute > vehicle.working_end_minute:
                violations.append(f"{stop.stop_id} past driver working hours")
    missing = set(order_by_id) - seen
    violations.extend(f"unassigned stop {stop_id}" for stop_id in sorted(missing))
    return tuple(violations)


def assign_orders_to_depots(
    orders: tuple[Order, ...], depots: tuple[Depot, ...]
) -> tuple[tuple[Order, ...], dict[str, str]]:
    """Attach each order to a serving depot.

    Preference order: an operational depot whose delivery area matches the
    order's area, otherwise the nearest operational depot. The chosen depot id
    and the reason for any area/proximity exception are recorded on the order.
    """
    if not depots:
        return orders, {}
    operational = [depot for depot in depots if depot.status.lower().startswith("operational")]
    pool = operational or list(depots)
    by_area: dict[str, Depot] = {}
    for depot in pool:
        if depot.delivery_area:
            by_area.setdefault(depot.delivery_area.casefold(), depot)
    assigned: list[Order] = []
    counts: dict[str, str] = {}
    for order in orders:
        nearest = min(pool, key=lambda depot: haversine_km(order.location, depot.location))
        area_match = by_area.get(order.delivery_area.casefold())
        if area_match is not None:
            chosen = area_match
            note = "" if chosen.depot_id == nearest.depot_id else "area-match over nearest depot"
        else:
            chosen = nearest
            note = f"no depot for area '{order.delivery_area}', used nearest"
        assigned.append(
            order.model_copy(update={"assigned_depot_id": chosen.depot_id, "assignment_note": note})
        )
        counts[chosen.depot_id] = counts.get(chosen.depot_id, "")
    return tuple(assigned), counts


def _sweep_into(
    vehicles: list[Vehicle],
    orders: list[Order],
    origin: Coordinate,
    buckets: dict[str, list[Order]],
) -> None:
    """Deterministic geographic sweep of ``orders`` across ``vehicles``.

    Appends into ``buckets`` (keyed by vehicle id) rather than returning, so it
    can be called repeatedly per time band.
    """
    if not vehicles or not orders:
        return
    sorted_orders = sorted(
        orders,
        key=lambda order: math.atan2(
            order.location.lat - origin.lat, order.location.lon - origin.lon
        ),
    )
    stops_per_vehicle = math.ceil(len(sorted_orders) / len(vehicles))
    for index, order in enumerate(sorted_orders):
        vehicle_index = min(index // stops_per_vehicle, len(vehicles) - 1)
        buckets[vehicles[vehicle_index].vehicle_id].append(order)


def _sweep_buckets(
    vehicles: tuple[Vehicle, ...], orders: list[Order], origin: Coordinate
) -> dict[str, list[Order]]:
    """Geographic sweep producing one order bucket per vehicle (time-blind)."""
    buckets: dict[str, list[Order]] = defaultdict(list)
    _sweep_into(list(vehicles), orders, origin, buckets)
    return buckets


def _vehicle_covers(
    vehicle: Vehicle, order: Order, *, enforce_delivery_windows: bool = True
) -> bool:
    """Whether the order's window overlaps the vehicle's working window."""
    window_start, window_end = effective_window(
        order, vehicle, enforce_delivery_windows=enforce_delivery_windows
    )
    return window_start <= vehicle.working_end_minute and window_end >= vehicle.working_start_minute


def _time_aware_buckets(
    vehicles: tuple[Vehicle, ...],
    orders: list[Order],
    origin: Coordinate,
    enforce_delivery_windows: bool = True,
) -> dict[str, list[Order]]:
    """Assign orders to vehicles by delivery time band, then geography.

    Orders are grouped by their delivery window. For each band, only vehicles
    whose working window covers that band are eligible, and the geographic sweep
    runs across those eligible vehicles. This keeps a vehicle from being handed a
    mix of morning and evening stops it cannot sequence within one shift, and
    balances load across vehicles that share a band. Orders whose band no vehicle
    can cover are left unbucketed and surface downstream as unassigned.

    When ``enforce_delivery_windows`` is false, orders have no time band at
    all — every vehicle is eligible for every order — so a single geographic
    sweep across the whole depot is used instead of banding by window.
    """
    buckets: dict[str, list[Order]] = defaultdict(list)
    if not vehicles or not orders:
        return buckets
    if not enforce_delivery_windows:
        _sweep_into(list(vehicles), orders, origin, buckets)
        return buckets
    bands: dict[tuple[int, int], list[Order]] = defaultdict(list)
    for order in orders:
        bands[(order.window_start_minute, order.window_end_minute)].append(order)
    # Process earliest windows first for determinism.
    for band in sorted(bands):
        band_orders = bands[band]
        eligible = [
            v
            for v in vehicles
            if _vehicle_covers(v, band_orders[0], enforce_delivery_windows=enforce_delivery_windows)
        ]
        if not eligible:
            # No vehicle can serve this window; leave the orders unbucketed.
            continue
        _sweep_into(eligible, band_orders, origin, buckets)
    return buckets


def build_plan(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    *,
    plan_id: str | None = None,
    version: int = 1,
    source_data_version: str = "fixture-v1",
    speed_kph_by_stop: dict[str, float] | None = None,
    depots: tuple[Depot, ...] = (),
    max_stops_per_vehicle: int = DEFAULT_MAX_STOPS_PER_VEHICLE,
    road_distance_km: dict[tuple[str, str], float] | None = None,
    enforce_delivery_windows: bool = True,
) -> PlanVersion:
    # When depots are supplied, plan per depot: each order is served by a
    # vehicle based at its assigned depot. Without depots we fall back to the
    # original single-origin sweep so synthetic fixtures keep working.
    if depots:
        assigned_orders, _ = assign_orders_to_depots(orders, depots)
        vehicles_by_depot: dict[str, list[Vehicle]] = defaultdict(list)
        for vehicle in vehicles:
            vehicles_by_depot[vehicle.depot_id].append(vehicle)
        orders_by_depot: dict[str, list[Order]] = defaultdict(list)
        for order in assigned_orders:
            orders_by_depot[order.assigned_depot_id].append(order)
        depot_origin = {depot.depot_id: depot.location for depot in depots}
        routes: list[VehicleRoute] = []
        for depot_id, depot_vehicles in vehicles_by_depot.items():
            depot_orders = orders_by_depot.get(depot_id, [])
            origin = depot_origin.get(depot_id, depot_vehicles[0].start)
            buckets = _time_aware_buckets(
                tuple(depot_vehicles), depot_orders, origin, enforce_delivery_windows
            )
            for vehicle in depot_vehicles:
                routes.append(
                    _build_route(
                        vehicle,
                        _ortools_order(
                            vehicle,
                            buckets[vehicle.vehicle_id],
                            speed_kph_by_stop,
                            max_stops_per_vehicle,
                            allow_drops=True,
                            road_distance_km=road_distance_km,
                            enforce_delivery_windows=enforce_delivery_windows,
                        ),
                        speed_kph_by_stop,
                        road_distance_km,
                        enforce_delivery_windows=enforce_delivery_windows,
                    )
                )
        planned_orders = assigned_orders
        route_tuple = tuple(routes)
    else:
        buckets = _sweep_buckets(vehicles, list(orders), vehicles[0].start)
        route_tuple = tuple(
            _build_route(
                vehicle,
                _ortools_order(
                    vehicle,
                    buckets[vehicle.vehicle_id],
                    speed_kph_by_stop,
                    max_stops_per_vehicle,
                    enforce_delivery_windows=enforce_delivery_windows,
                ),
                speed_kph_by_stop,
                enforce_delivery_windows=enforce_delivery_windows,
            )
            for vehicle in vehicles
        )
        planned_orders = orders
    provisional = PlanVersion(
        plan_id=plan_id or str(uuid4()),
        version=version,
        status="CANDIDATE",
        source_data_version=source_data_version,
        routes=route_tuple,
        objective_cost=round(sum(route.distance_km for route in route_tuple), 2),
    )
    violations = validate_plan(
        provisional,
        vehicles,
        planned_orders,
        max_stops_per_vehicle,
        enforce_delivery_windows=enforce_delivery_windows,
    )
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
