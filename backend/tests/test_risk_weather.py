from mahjourney.domain import Coordinate
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.planning import build_plan
from mahjourney.risk import monte_carlo_challenger
from mahjourney.weather import ForecastArea, RainfallStation, match_weather_feature


def test_monte_carlo_is_deterministic_and_rain_increases_tail() -> None:
    plan = build_plan(synthetic_fleet(), synthetic_orders())
    dry = monte_carlo_challenger(plan, samples=1000, rain_expected=False)
    wet = monte_carlo_challenger(plan, samples=1000, rain_expected=True)
    assert dry == monte_carlo_challenger(plan, samples=1000, rain_expected=False)
    assert wet["p90_finish_minutes"] > dry["p90_finish_minutes"]


def test_weather_matching_uses_nearest_station_and_forecast_label() -> None:
    midpoint = Coordinate(lat=1.32, lon=103.70)
    feature = match_weather_feature(
        "leg-1",
        midpoint,
        (
            RainfallStation("near", Coordinate(lat=1.321, lon=103.701), 1.2),
            RainfallStation("far", Coordinate(lat=1.40, lon=103.90), 0),
        ),
        (ForecastArea("Jurong", Coordinate(lat=1.319, lon=103.702), "Thundery Showers"),),
        ("rain-snapshot", "forecast-snapshot"),
    )
    assert feature.rainfall_station_id == "near"
    assert feature.wet_or_dry == "WET"
    assert feature.rain_expected
