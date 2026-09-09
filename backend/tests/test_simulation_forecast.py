import pytest

from mahjourney.domain import DisruptionEvent, DisruptionType
from mahjourney.forecasting import Transition, WeatherMarkovModel
from mahjourney.simulation import SimulationService


def test_simulation_replay_and_branch_are_deterministic() -> None:
    service = SimulationService()
    event = DisruptionEvent(
        event_id="event-fixed",
        scenario_id="demo",
        event_type=DisruptionType.HEAVY_RAIN,
        effective_minute=540,
    )
    service.inject(event)
    branch = service.branch("demo", 560)
    assert service.events(branch.scenario_id)[0].event_id == "event-fixed"
    assert service.events(branch.scenario_id)[0].effective_minute == 540
    assert service.get(branch.scenario_id).current_minute == 560


def test_live_mode_cannot_seek() -> None:
    service = SimulationService()
    service._clocks["live"] = service.get().model_copy(
        update={"scenario_id": "live", "mode": "LIVE"}
    )
    service._events["live"] = []
    with pytest.raises(ValueError):
        service.update("live", current_minute=600)


def test_markov_backoff_and_enablement_gate() -> None:
    model = WeatherMarkovModel(minimum_bucket_size=2)
    observation = Transition(3, 2, "WET", True, "PEAK", "WEEKDAY", "ARTERIAL")
    assert model.predict(observation) == (3, "PERSISTENCE")
    model.fit([observation, observation])
    assert model.predict(observation) == (2, "WEATHER_CONDITIONED")
    assert not model.enable_if_qualified(
        synchronized_days=13,
        traffic_brier=0.19,
        persistence_brier=0.20,
        weather_brier=0.18,
        traffic_eta_mae=8.0,
        weather_eta_mae=7.5,
    )
    assert model.enable_if_qualified(
        synchronized_days=14,
        traffic_brier=0.19,
        persistence_brier=0.20,
        weather_brier=0.18,
        traffic_eta_mae=8.0,
        weather_eta_mae=7.5,
    )
