from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from .domain import Coordinate, DisruptionEvent, SimulationClock
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
