from __future__ import annotations

from copy import deepcopy
from typing import Literal
from uuid import uuid4

from .domain import Coordinate, DisruptionEvent, SimulationClock, Vehicle, VehicleRoute
from .planning import haversine_km


class SimulationService:
    def __init__(self) -> None:
        self._clocks: dict[str, SimulationClock] = {"demo": SimulationClock()}
        self._events: dict[str, list[DisruptionEvent]] = {"demo": []}

    def get(self, scenario_id: str = "demo") -> SimulationClock:
        return self._clocks[scenario_id]

    def update(
        self,
        scenario_id: str,
        *,
        playing: bool | None = None,
        speed: int | None = None,
        current_minute: int | None = None,
    ) -> SimulationClock:
        clock = self._clocks[scenario_id]
        if clock.mode == "LIVE" and current_minute is not None:
            raise ValueError("live mode cannot seek")
        updates = {}
        if playing is not None:
            updates["playing"] = playing
        if speed is not None:
            if speed not in (1, 5, 20):
                raise ValueError("speed must be 1, 5, or 20")
            updates["speed"] = speed
        if current_minute is not None:
            updates["current_minute"] = max(0, min(1439, current_minute))
        self._clocks[scenario_id] = clock.model_copy(update=updates)
        return self._clocks[scenario_id]

    def reset(self, scenario_id: str) -> SimulationClock:
        parent = self._clocks[scenario_id].branch_parent_id
        self._clocks[scenario_id] = SimulationClock(
            scenario_id=scenario_id, branch_parent_id=parent
        )
        return self._clocks[scenario_id]

    def inject(self, event: DisruptionEvent) -> DisruptionEvent:
        if self._clocks[event.scenario_id].mode != "SCENARIO":
            raise ValueError("disruptions can only be injected in scenario mode")
        self._events[event.scenario_id].append(event)
        self._events[event.scenario_id].sort(
            key=lambda item: (item.effective_minute, item.event_id)
        )
        return event

    def events(self, scenario_id: str) -> tuple[DisruptionEvent, ...]:
        return tuple(self._events[scenario_id])

    def branch(self, scenario_id: str, at_minute: int) -> SimulationClock:
        parent_clock = self._clocks[scenario_id]
        if parent_clock.mode != "SCENARIO":
            raise ValueError("live mode cannot branch")
        branch_id = f"branch-{uuid4().hex[:8]}"
        self._clocks[branch_id] = SimulationClock(
            scenario_id=branch_id,
            current_minute=at_minute,
            branch_parent_id=scenario_id,
        )
        self._events[branch_id] = [
            deepcopy(event).model_copy(update={"scenario_id": branch_id})
            for event in self._events[scenario_id]
            if event.effective_minute <= at_minute
        ]
        return self._clocks[branch_id]


def interpolated_progress(current_minute: int, departure: int, arrival: int) -> float:
    if current_minute <= departure:
        return 0.0
    if current_minute >= arrival:
        return 1.0
    return (current_minute - departure) / max(1, arrival - departure)


def point_along_geometry(geometry: tuple[Coordinate, ...], progress: float) -> Coordinate:
    if not geometry:
        raise ValueError("geometry cannot be empty")
    if len(geometry) == 1 or progress <= 0:
        return geometry[0]
    if progress >= 1:
        return geometry[-1]
    lengths = [
        haversine_km(start, end)
        for start, end in zip(geometry, geometry[1:], strict=False)
    ]
    target = sum(lengths) * progress
    traversed = 0.0
    for index, length in enumerate(lengths):
        if traversed + length >= target:
            local = (target - traversed) / max(length, 1e-9)
            start = geometry[index]
            end = geometry[index + 1]
            return Coordinate(
                lat=start.lat + (end.lat - start.lat) * local,
                lon=start.lon + (end.lon - start.lon) * local,
            )
        traversed += length
    return geometry[-1]


def _build_cumulative(polyline: tuple[Coordinate, ...]) -> tuple[list[float], float]:
    """Return (cum, total) for a polyline.

    ``cum[i]`` is the haversine distance from polyline[0] to polyline[i].
    ``total`` is the full length.  Returns ([0.0], 0.0) for a single point.
    """
    cum: list[float] = [0.0]
    for i in range(len(polyline) - 1):
        cum.append(cum[-1] + haversine_km(polyline[i], polyline[i + 1]))
    return cum, cum[-1]


def _fraction_of_stop(stop_location: Coordinate, polyline: tuple[Coordinate, ...]) -> float:
    """The fraction [0, 1] along ``polyline`` where ``stop_location`` sits.

    Projects the stop onto every segment of the polyline and picks the
    segment whose foot-point is closest.  Returns the cumulative distance
    to that foot-point divided by the total polyline length.

    Falls back to 0.5 for a degenerate (single-point or zero-length) polyline
    so callers never receive an out-of-range value.
    """
    cum, total = _build_cumulative(polyline)
    if total < 1e-9 or len(polyline) < 2:
        return 0.5

    best_dist = float("inf")
    best_frac = 0.0

    for i in range(len(polyline) - 1):
        a = polyline[i]
        b = polyline[i + 1]
        seg_len = haversine_km(a, b)

        # Project stop onto the segment a->b using the dot-product formula in
        # lat/lon space (acceptable approximation over short city-scale segments).
        if seg_len < 1e-9:
            # Zero-length segment: snap to the segment start.
            t = 0.0
        else:
            ax, ay = a.lon, a.lat
            bx, by = b.lon, b.lat
            px, py = stop_location.lon, stop_location.lat
            t = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / (
                (bx - ax) ** 2 + (by - ay) ** 2
            )
            t = max(0.0, min(1.0, t))

        foot = Coordinate(
            lat=a.lat + (b.lat - a.lat) * t,
            lon=a.lon + (b.lon - a.lon) * t,
        )
        dist = haversine_km(stop_location, foot)
        if dist < best_dist:
            best_dist = dist
            best_frac = (cum[i] + t * seg_len) / total

    return best_frac


VehiclePhase = Literal["STANDBY", "AT_DEPOT", "EN_ROUTE", "SERVICING"]


class VehicleProgress:
    """Where a vehicle is on its route at a scenario minute, and which stops are
    done / committed / still re-sequenceable.

    This is the single source of truth for both the map dot (``/map/state``) and
    the mid-route reroute seed, so the two can never disagree about position or
    which stops remain.

    The dot's geometry fraction determines which stops are completed: a stop is
    considered done once the dot has passed its projected position along the route
    polyline.  The reroute planner still reads ETAs from the plan (for timing the
    re-sequenced tail), but uses this geometry-fraction split to decide which stops
    those ETAs belong to.

    Fields:
    - ``position``            — the dot: where the vehicle is right now.
    - ``phase``               — STANDBY / AT_DEPOT / EN_ROUTE.
    - ``completed_stop_ids``  — stops whose geometry fraction < dot fraction.
    - ``committed_stop_id``   — next stop ahead of the dot (pinned, not re-seq'd).
    - ``remaining_stop_ids``  — stops after the committed stop (re-sequenceable).
    - ``current_leg``         — (origin, destination) of the leg the dot is on,
                                used by the reroute case-(a) closure check.
    """

    __slots__ = (
        "position",
        "phase",
        "completed_stop_ids",
        "committed_stop_id",
        "remaining_stop_ids",
        "current_leg",
    )

    def __init__(
        self,
        *,
        position: Coordinate,
        phase: VehiclePhase,
        completed_stop_ids: tuple[str, ...],
        committed_stop_id: str | None,
        remaining_stop_ids: tuple[str, ...],
        current_leg: tuple[Coordinate, Coordinate] | None,
    ) -> None:
        self.position = position
        self.phase = phase
        self.completed_stop_ids = completed_stop_ids
        self.committed_stop_id = committed_stop_id
        self.remaining_stop_ids = remaining_stop_ids
        self.current_leg = current_leg

    @property
    def completed_count(self) -> int:
        return len(self.completed_stop_ids)


def _route_polyline(route: VehicleRoute, depot: Coordinate) -> tuple[Coordinate, ...]:
    """The polyline used for both dot positioning and stop-fraction projection.

    Prefers the road geometry when available (depot -> road vertices -> last stop).
    Falls back to the straight-line waypoint chain depot -> stop1 -> ... -> lastStop.
    """
    if route.geometry and len(route.geometry) >= 2:
        return route.geometry
    return (depot,) + tuple(s.location for s in route.stops)


def vehicle_progress(
    route: VehicleRoute, vehicle: Vehicle, current_minute: int
) -> VehicleProgress:
    """Derive where ``vehicle`` is on ``route`` at ``current_minute``.

    Single source of truth for both the map dot and the reroute seed.

    Position model (smooth, no jumps):
    - The dot moves uniformly from shift_start to last_departure along the
      full route polyline.  No ETA involvement — the dot never snaps to a
      stop location or pauses during service.

    Stop-completion model (geometry-fraction driven):
    - Each stop's position is projected onto the route polyline to get its
      fraction along the route.
    - A stop is completed once the dot's current fraction >= that stop's
      fraction.  This means the dot passing a stop on the map is exactly
      what marks it done — the reroute planner and the visual are in sync.
    - ETAs from the plan are still used for the committed stop's departure
      time (the seed minute for re-sequencing the tail).
    """
    depot = vehicle.start
    stops = route.stops

    # Reserve vehicle — no stops at all.
    if not stops:
        return VehicleProgress(
            position=depot,
            phase="STANDBY",
            completed_stop_ids=(),
            committed_stop_id=None,
            remaining_stop_ids=(),
            current_leg=None,
        )

    shift_start = vehicle.working_start_minute
    route_end = stops[-1].departure_minute

    # --- Dot position (uniform along full polyline) -----------------------
    polyline = _route_polyline(route, depot)

    if current_minute <= shift_start:
        dot_fraction = 0.0
    elif current_minute >= route_end:
        dot_fraction = 1.0
    else:
        dot_fraction = (current_minute - shift_start) / max(1, route_end - shift_start)

    position = point_along_geometry(polyline, dot_fraction)

    # Before the vehicle departs: sitting at depot.
    if dot_fraction == 0.0:
        first_stop = stops[0]
        remaining = tuple(s.stop_id for s in stops[1:])
        return VehicleProgress(
            position=depot,
            phase="AT_DEPOT",
            completed_stop_ids=(),
            committed_stop_id=first_stop.stop_id,
            remaining_stop_ids=remaining,
            current_leg=(depot, first_stop.location),
        )

    # --- Stop fractions: project each stop onto the polyline --------------
    stop_fractions = [_fraction_of_stop(s.location, polyline) for s in stops]

    # Completed = stops whose fraction the dot has already passed.
    completed_ids: list[str] = []
    ahead_indices: list[int] = []
    for i, stop in enumerate(stops):
        if stop_fractions[i] <= dot_fraction:
            completed_ids.append(stop.stop_id)
        else:
            ahead_indices.append(i)

    # All stops passed — route done, dot sits at last stop.
    if not ahead_indices:
        return VehicleProgress(
            position=position,
            phase="EN_ROUTE",
            completed_stop_ids=tuple(completed_ids),
            committed_stop_id=None,
            remaining_stop_ids=(),
            current_leg=None,
        )

    # Committed stop = first stop still ahead of the dot.
    committed_index = ahead_indices[0]
    committed_stop = stops[committed_index]
    remaining = tuple(stops[i].stop_id for i in ahead_indices[1:])

    # current_leg origin: last completed stop, or depot if none completed yet.
    if completed_ids:
        # Find the last completed stop in sequence order.
        last_done = next(
            s for s in reversed(stops) if s.stop_id in set(completed_ids)
        )
        leg_origin: Coordinate = last_done.location
    else:
        leg_origin = depot

    return VehicleProgress(
        position=position,
        phase="EN_ROUTE",
        completed_stop_ids=tuple(completed_ids),
        committed_stop_id=committed_stop.stop_id,
        remaining_stop_ids=remaining,
        current_leg=(leg_origin, committed_stop.location),
    )
