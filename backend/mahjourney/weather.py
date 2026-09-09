from __future__ import annotations

from dataclasses import dataclass

from .domain import Coordinate, RouteWeatherFeature
from .planning import haversine_km


@dataclass(frozen=True)
class RainfallStation:
    station_id: str
    location: Coordinate
    rainfall_mm: float


@dataclass(frozen=True)
class ForecastArea:
    name: str
    label_location: Coordinate
    forecast: str


def match_weather_feature(
    route_leg_id: str,
    midpoint: Coordinate,
    stations: tuple[RainfallStation, ...],
    areas: tuple[ForecastArea, ...],
    source_snapshot_ids: tuple[str, ...],
) -> RouteWeatherFeature:
    station = min(stations, key=lambda item: haversine_km(midpoint, item.location), default=None)
    area = min(areas, key=lambda item: haversine_km(midpoint, item.label_location), default=None)
    rain_terms = ("rain", "shower", "thunderstorm")
    return RouteWeatherFeature(
        route_leg_id=route_leg_id,
        rainfall_station_id=station.station_id if station else None,
        forecast_area=area.name if area else None,
        wet_or_dry="UNKNOWN" if station is None else ("WET" if station.rainfall_mm > 0 else "DRY"),
        rain_expected=bool(area and any(term in area.forecast.casefold() for term in rain_terms)),
        source_snapshot_ids=source_snapshot_ids,
    )
