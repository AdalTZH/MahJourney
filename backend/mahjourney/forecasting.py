from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Transition:
    current_band: int
    next_band: int
    wet_or_dry: str
    rain_expected: bool
    time_of_day: str
    day_type: str
    road_category: str


class WeatherMarkovModel:
    def __init__(self, minimum_bucket_size: int = 100) -> None:
        self.minimum_bucket_size = minimum_bucket_size
        self._weather: dict[tuple[object, ...], Counter[int]] = defaultdict(Counter)
        self._traffic: dict[tuple[object, ...], Counter[int]] = defaultdict(Counter)
        self.observation_count = 0
        self.operational_enabled = False

    def fit(self, transitions: Iterable[Transition]) -> None:
        for transition in transitions:
            weather_key = (
                transition.current_band,
                transition.wet_or_dry,
                transition.rain_expected,
                transition.time_of_day,
                transition.day_type,
                transition.road_category,
            )
            traffic_key = (
                transition.current_band,
                transition.time_of_day,
                transition.day_type,
                transition.road_category,
            )
            self._weather[weather_key][transition.next_band] += 1
            self._traffic[traffic_key][transition.next_band] += 1
            self.observation_count += 1

    def predict(self, transition: Transition) -> tuple[int, str]:
        weather_key = (
            transition.current_band,
            transition.wet_or_dry,
            transition.rain_expected,
            transition.time_of_day,
            transition.day_type,
            transition.road_category,
        )
        traffic_key = (
            transition.current_band,
            transition.time_of_day,
            transition.day_type,
            transition.road_category,
        )
        if sum(self._weather[weather_key].values()) >= self.minimum_bucket_size:
            return self._weather[weather_key].most_common(1)[0][0], "WEATHER_CONDITIONED"
        if sum(self._traffic[traffic_key].values()) >= self.minimum_bucket_size:
            return self._traffic[traffic_key].most_common(1)[0][0], "TRAFFIC_ONLY"
        return transition.current_band, "PERSISTENCE"

    def enable_if_qualified(
        self,
        *,
        synchronized_days: int,
        traffic_brier: float,
        persistence_brier: float,
        weather_brier: float,
        traffic_eta_mae: float,
        weather_eta_mae: float,
    ) -> bool:
        self.operational_enabled = (
            synchronized_days >= 14
            and traffic_brier < persistence_brier
            and weather_brier < traffic_brier
            and weather_eta_mae < traffic_eta_mae
        )
        return self.operational_enabled
