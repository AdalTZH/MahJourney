from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import uuid4

from .disruptions import path_intersects_polygon
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

logger = logging.getLogger(__name__)

# Leg-level closure avoidance (R10). A leg whose straight source->target segment
# crosses an active closure zone is penalized so the solver sequences around it.
# HARD: a prohibitive per-leg cost (well above any achievable real route cost,
# same order of magnitude as the drop penalty) so the solver only uses a crossing
# leg if it literally has no closure-free alternative. SOFT: a large multiplier
# used on a fallback re-solve when EVERY ordering must cross, so a candidate is
# still produced rather than failing. Distances/times are in km/minutes at city
# scale, so these constants dwarf any real leg.
CLOSURE_HARD_LEG_PENALTY = 1_000_000
CLOSURE_SOFT_LEG_MULTIPLIER = 1000

# Realistic capacity dimensions are scaled to integers because OR-Tools capacity
# dimensions operate on integers. Grams and litres keep enough precision.
_WEIGHT_SCALE = 1000  # kg -> grams
_VOLUME_SCALE = 1000  # m3 -> litres
DEFAULT_MAX_STOPS_PER_VEHICLE = 25
# Default OR-Tools search budget (seconds) when a caller does not pass one.
# Kept in the 2-10s range recommended for real depot sizes; the previous
# hardcoded 250ms rarely let the metaheuristic improve on the first solution.
DEFAULT_ORTOOLS_TIME_LIMIT_SECONDS = 5


@dataclass
class SolverStats:
    """Per-plan accounting of how each vehicle's route was solved.

    ``build_plan`` passes one instance down through ``_ortools_order`` so that,
    after a plan is built, we can log which solver path actually produced each
    route. ``ortools`` counts routes solved by OR-Tools, ``fallback`` counts
    routes that dropped to the deterministic nearest-neighbor path, and
    ``fallback_reasons`` records the distinct exception/None causes so a silent
    degradation to greedy construction is visible instead of hidden.
    """

    ortools: int = 0
    fallback: int = 0
    fallback_reasons: list[str] = field(default_factory=list)

    def record_ortools(self) -> None:
        self.ortools += 1

    def record_fallback(self, reason: str) -> None:
        self.fallback += 1
        self.fallback_reasons.append(reason)

    @property
    def total(self) -> int:
        return self.ortools + self.fallback


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


def leg_crosses_closure(
    a: Coordinate,
    b: Coordinate,
    closure_polygons: tuple[tuple[Coordinate, ...], ...] | None,
) -> bool:
    """Whether the straight leg ``a -> b`` enters or crosses any closure zone.

    Reuses the same geometry predicate the disruption engine uses to decide
    whether a route path is affected, applied to a single leg's straight
    segment. Returns ``False`` when there are no closure polygons, so the
    default (no-closure) planning path pays nothing.
    """
    if not closure_polygons:
        return False
    segment = (a, b)
    return any(path_intersects_polygon(segment, polygon) for polygon in closure_polygons)


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
    *,
    start_coordinate: Coordinate | None = None,
    start_node_id: str | None = None,
    start_minute: int | None = None,
    closure_polygons: tuple[tuple[Coordinate, ...], ...] | None = None,
) -> list[Order]:
    remaining = list(orders)
    # Origin defaults to the depot at shift start; a mid-route reroute overrides
    # it to the vehicle's current position and time (see _ortools_order).
    current = start_coordinate if start_coordinate is not None else vehicle.start
    current_id = start_node_id if start_node_id is not None else depot_node_id(vehicle.start)
    start_minute = start_minute if start_minute is not None else vehicle.working_start_minute
    route: list[Order] = []
    while remaining:
        feasible = [order for order in remaining if _fits(vehicle, route, order, max_stops)]
        if not feasible:
            break
        # Leg-level closure avoidance (R10): a leg into ``order`` that crosses a
        # closure sorts after every non-crossing option (the leading 0/1 key), so
        # a crossing leg is only chosen when no closure-free feasible order
        # remains — mirroring the OR-Tools hard-exclusion-then-soft behavior.
        chosen = min(
            feasible,
            key=lambda order: (
                1 if leg_crosses_closure(current, order.location, closure_polygons) else 0,
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
    time_limit_seconds: float = DEFAULT_ORTOOLS_TIME_LIMIT_SECONDS,
    stats: SolverStats | None = None,
    *,
    start_coordinate: Coordinate | None = None,
    start_node_id: str | None = None,
    start_minute: int | None = None,
    closure_polygons: tuple[tuple[Coordinate, ...], ...] | None = None,
) -> list[Order]:
    """Solve one route under weight/volume/time/working-hours/max-stops constraints.

    When ``road_distance_km`` is supplied, leg costs/times use real OneMap road
    distance so the visit order is optimized on driving distance; missing pairs
    fall back to haversine. Retains a deterministic capacity-aware
    nearest-neighbor fallback so the system still plans when OR-Tools is
    unavailable or fails to converge. When ``enforce_delivery_windows`` is
    false, every order's effective window is the vehicle's own working
    window, so orders can be served any time the vehicle/driver is available.

    OR-Tools searches with GUIDED_LOCAL_SEARCH for up to ``time_limit_seconds``,
    so it can improve on the greedy first solution instead of returning it as-is.
    Whenever the search falls back to nearest-neighbor — OR-Tools missing, an
    error, or no solution found within the budget — that is recorded on
    ``stats`` (if given) and logged at WARNING level so the degradation to
    greedy construction is never silent.

    ``start_coordinate`` / ``start_node_id`` / ``start_minute`` override the
    route's origin and departure time. When omitted (the default) the origin is
    the depot at the vehicle's shift start — exactly today's behavior for every
    existing caller. A mid-route reroute passes the vehicle's current position
    and current time so node 0 is seeded there instead of the depot; the route
    still terminates at the depot. The fallback carries the same override.

    ``closure_polygons`` (R10) makes the solver sequence AROUND a road closure:
    any leg whose straight source->target segment crosses a closure zone is
    penalized. A two-pass scheme first HARD-excludes crossing legs (prohibitive
    cost), then, only if that leaves no solution (every ordering must cross),
    re-solves with a large SOFT multiplier so a candidate is still produced. With
    no closures, cost is exactly today's. This shapes the visit ORDER only; it
    does not bend an individual leg's road geometry (that needs an avoid-area
    provider and is out of scope).
    """
    if not orders:
        return []

    # Origin overrides default to the depot at shift start (today's behavior).
    origin_coordinate = start_coordinate if start_coordinate is not None else vehicle.start
    origin_node_id = start_node_id if start_node_id is not None else depot_node_id(vehicle.start)
    start_minute = start_minute if start_minute is not None else vehicle.working_start_minute

    def _fallback(reason: str) -> list[Order]:
        logger.warning(
            "route solver fell back to nearest-neighbor: vehicle=%s stops=%d reason=%s",
            vehicle.vehicle_id,
            len(orders),
            reason,
        )
        if stats is not None:
            stats.record_fallback(reason)
        return _nearest_neighbor(
            vehicle,
            orders,
            max_stops,
            road_distance_km,
            enforce_delivery_windows,
            start_coordinate=start_coordinate,
            start_node_id=start_node_id,
            start_minute=start_minute,
            closure_polygons=closure_polygons,
        )

    end_minute = max(start_minute + 1, vehicle.working_end_minute)
    horizon = max(1, end_minute - start_minute)
    # Node 0 is the route origin (depot by default, or the mid-route start).
    node_ids = [origin_node_id, *(order.order_id for order in orders)]
    # When the origin is NOT the depot (a mid-route reroute), the route must
    # still finish at the depot rather than back at the mid-route start, so we
    # append a distinct depot end-node and give the solver an explicit end. In
    # the default (depot origin) case, start == end == node 0, which is exactly
    # today's single-node-0 behavior.
    reroute_origin = start_coordinate is not None
    if reroute_origin:
        depot_end_node = len(orders) + 1
        node_ids.append(depot_node_id(vehicle.start))
        end_coordinate = vehicle.start
    else:
        depot_end_node = 0
        end_coordinate = origin_coordinate
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2

        locations = [origin_coordinate, *(order.location for order in orders)]

        def _is_order_node(node: int) -> bool:
            # Node 0 is the origin; the appended depot_end_node (reroute case) is
            # the terminus. Both carry no order demand/service.
            return node != 0 and node != depot_end_node

        def _solve_with_leg_penalty(leg_penalty: int):
            """Build and solve the model once; ``leg_penalty`` is added to the
            cost of any leg crossing a closure zone (R10). Returns the ordered
            order list, or ``None`` if OR-Tools found no solution in the budget.

            Called twice by the two-pass scheme: first with a prohibitive HARD
            penalty (so the solver avoids closure legs entirely when it can),
            then, only if that yields no solution, with a large SOFT multiplier
            so a candidate is still produced when every ordering must cross.
            ``leg_penalty == 0`` (no closures) is exactly today's cost model.
            """
            solve_locations = list(locations)
            if reroute_origin:
                solve_locations.append(end_coordinate)
                manager = pywrapcp.RoutingIndexManager(
                    len(solve_locations), 1, [0], [depot_end_node]
                )
            else:
                manager = pywrapcp.RoutingIndexManager(len(solve_locations), 1, 0)
            routing = pywrapcp.RoutingModel(manager)

            def transit(from_index: int, to_index: int) -> int:
                source_node = manager.IndexToNode(from_index)
                target_node = manager.IndexToNode(to_index)
                source = solve_locations[source_node]
                target = solve_locations[target_node]
                speed = (
                    speed_kph_by_stop.get(orders[target_node - 1].order_id, 28.0)
                    if speed_kph_by_stop and _is_order_node(target_node)
                    else 28.0
                )
                service = (
                    math.ceil(order_service_seconds(orders[target_node - 1]) / 60)
                    if _is_order_node(target_node)
                    else 0
                )
                travel = leg_travel_minutes(
                    node_ids[source_node], node_ids[target_node], source, target, speed,
                    road_distance_km,
                )
                penalty = (
                    leg_penalty
                    if leg_penalty and leg_crosses_closure(source, target, closure_polygons)
                    else 0
                )
                return travel + service + penalty

            transit_index = routing.RegisterTransitCallback(transit)
            routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
            # Time dimension spans the whole shift; the depot start is pinned to
            # the working-hours start and every served node must respect its window.
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
                        # Force-drop: make the node mandatory-to-skip by giving it
                        # an empty feasible window plus a disjunction so the solver
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
                return (
                    round(orders[node - 1].weight_kg * _WEIGHT_SCALE)
                    if _is_order_node(node) else 0
                )

            def volume_demand(from_index: int) -> int:
                node = manager.IndexToNode(from_index)
                return (
                    round(orders[node - 1].volume_m3 * _VOLUME_SCALE)
                    if _is_order_node(node) else 0
                )

            def stop_demand(from_index: int) -> int:
                return 1 if _is_order_node(manager.IndexToNode(from_index)) else 0

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
            search.first_solution_strategy = (
                routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
            )
            # Guided local search escapes the local minimum the cheapest-arc first
            # solution lands in, so the extra time budget actually buys shorter
            # routes rather than re-confirming the greedy construction.
            search.local_search_metaheuristic = (
                routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
            )
            # GLS never terminates on its own, so a wall-clock budget bounds the
            # search. Clamp to at least 1ms so a misconfigured 0 still runs.
            search.time_limit.FromMilliseconds(max(1, round(time_limit_seconds * 1000)))
            solution = routing.SolveWithParameters(search)
            if solution is None:
                return None
            ordered: list[Order] = []
            index = routing.Start(0)
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if _is_order_node(node):
                    ordered.append(orders[node - 1])
                index = solution.Value(routing.NextVar(index))
            return ordered

        # Two-pass leg-level closure avoidance (R10). First pass hard-excludes any
        # leg crossing a closure so the solver sequences around it. If that yields
        # no solution (every ordering must cross the closure), retry with a large
        # SOFT multiplier so a candidate is still produced rather than failing.
        # With no closures, the first pass uses leg_penalty 0 — today's cost model.
        first_penalty = CLOSURE_HARD_LEG_PENALTY if closure_polygons else 0
        result = _solve_with_leg_penalty(first_penalty)
        if result is None and closure_polygons:
            logger.info(
                "closure hard-exclusion left no solution for vehicle=%s; "
                "retrying with soft leg penalty",
                vehicle.vehicle_id,
            )
            result = _solve_with_leg_penalty(CLOSURE_SOFT_LEG_MULTIPLIER)
        if result is None:
            return _fallback("no_solution_within_time_limit")
        if stats is not None:
            stats.record_ortools()
        return result
    except (ImportError, RuntimeError, ValueError) as exc:
        return _fallback(f"{type(exc).__name__}: {exc}")


def _build_route(
    vehicle: Vehicle,
    orders: list[Order],
    speed_kph_by_stop: dict[str, float] | None = None,
    road_distance_km: dict[tuple[str, str], float] | None = None,
    enforce_delivery_windows: bool = True,
    *,
    start_coordinate: Coordinate | None = None,
    start_node_id: str | None = None,
    start_minute: int | None = None,
    closure_polygons: tuple[tuple[Coordinate, ...], ...] | None = None,
) -> VehicleRoute:
    # Origin defaults to the depot at shift start (today's behavior). A mid-route
    # reroute overrides these to the vehicle's current position and time, so the
    # rebuilt tail's ETAs are computed from "now", not from the shift start. The
    # route still returns to the depot (vehicle.start) at the end.
    #
    # ``closure_polygons`` (R10) does NOT alter the physical distance/time this
    # function reports — those must stay real km/minutes. It is used only to
    # surface whether the chosen sequence still traverses a closure (logged), so
    # this stays consistent with the solver, which already sequenced to avoid
    # crossings. The solver's cost penalty shapes the ORDER; _build_route reports
    # the true cost of that order.
    minute = start_minute if start_minute is not None else vehicle.working_start_minute
    current = start_coordinate if start_coordinate is not None else vehicle.start
    current_id = start_node_id if start_node_id is not None else depot_node_id(vehicle.start)
    crossed_closure = False
    distance = 0.0
    # Moment the vehicle actually rolls out toward its first stop. A vehicle whose
    # first delivery window opens later waits rather than departing early, so
    # leading idle time is excluded from duration.
    depart_depot_minute: int | None = None
    stops: list[RouteStop] = []
    for sequence, order in enumerate(orders, start=1):
        if leg_crosses_closure(current, order.location, closure_polygons):
            crossed_closure = True
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
    if leg_crosses_closure(current, vehicle.start, closure_polygons):
        crossed_closure = True
    distance += leg_distance_km(current_id, return_id, current, vehicle.start, road_distance_km)
    minute += leg_travel_minutes(
        current_id, return_id, current, vehicle.start, 28.0, road_distance_km
    )
    if crossed_closure:
        # The chosen sequence still traverses a closure zone. This is expected
        # only when avoidance was impossible (soft-fallback in _ortools_order);
        # surfaced here so a route that unavoidably drives through a closure is
        # visible rather than silent.
        logger.info(
            "built route for vehicle=%s traverses a closure zone (avoidance not possible)",
            vehicle.vehicle_id,
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
        rebuilt = _build_route(
            vehicle,
            sequence_orders,
            speed_kph_by_stop,
            enforce_delivery_windows=enforce_delivery_windows,
        )
        # Re-timing keeps the same stops and sequence, so the road geometry is
        # still valid — carry it over. _build_route doesn't set geometry (that's
        # OneMap enrichment), so without this every route line would lose its
        # geometry and disappear from the map after a live disruption re-time.
        routes.append(rebuilt.model_copy(update={"geometry": route.geometry}))
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
    ortools_time_limit_seconds: float | None = None,
) -> PlanVersion:
    # When the caller doesn't pin a budget, resolve it from settings so a single
    # env var (ORTOOLS_TIME_LIMIT_SECONDS) governs every plan build — including
    # the many direct build_plan() calls in tests, which is what lets the test
    # env drop the budget to keep CI fast. Falls back to the module default if
    # settings can't be loaded (keeps planning importable in isolation).
    if ortools_time_limit_seconds is None:
        try:
            from .config import get_settings

            ortools_time_limit_seconds = float(get_settings().ortools_time_limit_seconds)
        except Exception:
            ortools_time_limit_seconds = DEFAULT_ORTOOLS_TIME_LIMIT_SECONDS
    # Accumulates which solver path produced each route so the quality summary
    # below can report OR-Tools vs. nearest-neighbor usage for this plan.
    stats = SolverStats()

    def _per_solve_limit(buckets: dict[str, list[Order]]) -> float:
        """Split the plan-wide OR-Tools budget across the routes being solved.

        GUIDED_LOCAL_SEARCH runs until its wall-clock limit expires — it never
        stops early — so a per-vehicle limit would make total plan latency scale
        with fleet size. Treating ``ortools_time_limit_seconds`` as a budget for
        the whole plan and dividing it across the vehicles that actually have
        stops keeps end-to-end plan time roughly constant regardless of fleet
        size. A 0.25s floor keeps each solve meaningful for large fleets.
        """
        solves = sum(1 for stops in buckets.values() if stops)
        if solves <= 1:
            return ortools_time_limit_seconds
        return max(0.25, ortools_time_limit_seconds / solves)
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
        # Build every depot's buckets first so the OR-Tools budget can be split
        # across all routes in the whole plan, not reset per depot.
        buckets_by_depot: dict[str, dict[str, list[Order]]] = {}
        for depot_id, depot_vehicles in vehicles_by_depot.items():
            depot_orders = orders_by_depot.get(depot_id, [])
            origin = depot_origin.get(depot_id, depot_vehicles[0].start)
            buckets_by_depot[depot_id] = _time_aware_buckets(
                tuple(depot_vehicles), depot_orders, origin, enforce_delivery_windows
            )
        all_buckets: dict[str, list[Order]] = {
            vid: stops
            for depot_buckets in buckets_by_depot.values()
            for vid, stops in depot_buckets.items()
        }
        per_solve = _per_solve_limit(all_buckets)
        routes: list[VehicleRoute] = []
        for depot_id, depot_vehicles in vehicles_by_depot.items():
            buckets = buckets_by_depot[depot_id]
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
                            time_limit_seconds=per_solve,
                            stats=stats,
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
        per_solve = _per_solve_limit(buckets)
        route_tuple = tuple(
            _build_route(
                vehicle,
                _ortools_order(
                    vehicle,
                    buckets[vehicle.vehicle_id],
                    speed_kph_by_stop,
                    max_stops_per_vehicle,
                    enforce_delivery_windows=enforce_delivery_windows,
                    time_limit_seconds=per_solve,
                    stats=stats,
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
    _log_plan_quality(provisional, vehicles, orders, stats)
    return provisional.model_copy(
        update={
            "status": "VALIDATED" if not violations else "CANDIDATE",
            "hard_violations": violations,
        }
    )


def _log_plan_quality(
    plan: PlanVersion,
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    stats: SolverStats,
) -> None:
    """Log how well this plan was solved and how it compares to naive greedy.

    Emits, at INFO, the objective_cost, the greedy_baseline cost, the percent
    improvement over that baseline, and the OR-Tools-vs-fallback route split so
    a plan that was merely constructed greedily (rather than optimized) is
    visible in the logs. Any fallback reasons are included so a silent
    degradation is traceable. This is measurement only — it never changes the
    plan — and is wrapped defensively so instrumentation can never break
    planning.
    """
    try:
        baseline_cost = greedy_baseline(vehicles, orders).objective_cost if orders else 0.0
        improvement = (
            (baseline_cost - plan.objective_cost) / baseline_cost * 100.0
            if baseline_cost > 0
            else 0.0
        )
        logger.info(
            "plan quality: plan_id=%s cost=%.2f greedy_baseline=%.2f improvement=%.1f%% "
            "routes_ortools=%d routes_fallback=%d",
            plan.plan_id,
            plan.objective_cost,
            baseline_cost,
            improvement,
            stats.ortools,
            stats.fallback,
        )
        if stats.fallback:
            logger.warning(
                "plan %s used nearest-neighbor for %d/%d route(s); reasons=%s",
                plan.plan_id,
                stats.fallback,
                stats.total,
                ", ".join(sorted(set(stats.fallback_reasons))),
            )
    except Exception as exc:  # instrumentation must never break planning
        logger.debug("plan quality logging skipped: %s", exc)


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
