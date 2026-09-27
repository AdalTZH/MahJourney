"""Trigger-wiring tests for _handle_disruption_effect (spec task 8).

Verifies the branch decision without needing full app state: a ROAD_CLOSURE
attempts a mid-route reroute (and, when a candidate is produced, does NOT also
re-time in place); a non-closure disruption, or a closure that yields no
candidate, falls back to re-time-in-place.
"""

from __future__ import annotations

import pytest

from mahjourney import api
from mahjourney.domain import DisruptionEvent, DisruptionType, PlanVersion


def _event(event_type: DisruptionType) -> DisruptionEvent:
    return DisruptionEvent(
        scenario_id="demo", event_type=event_type, effective_minute=480, payload={},
    )


def _candidate() -> PlanVersion:
    return PlanVersion(
        plan_id="plan-1", version=4, status="CANDIDATE",
        source_data_version="mid-route-reroute:evt-1+x", routes=(), objective_cost=0.0,
    )


@pytest.mark.asyncio
async def test_road_closure_with_candidate_does_not_retime(monkeypatch) -> None:
    calls = {"reroute": 0, "retime": 0}

    async def fake_reroute(state, scenario_id, event):
        calls["reroute"] += 1
        return _candidate()

    async def fake_retime(state, scenario_id):
        calls["retime"] += 1

    monkeypatch.setattr(api, "_reroute_affected_vehicles", fake_reroute)
    monkeypatch.setattr(api, "_apply_disruption_to_live_plan", fake_retime)

    event = _event(DisruptionType.ROAD_CLOSURE)
    result = await api._handle_disruption_effect(object(), "demo", event)
    assert result is not None
    assert calls == {"reroute": 1, "retime": 0}


@pytest.mark.asyncio
async def test_road_closure_no_candidate_falls_back_to_retime(monkeypatch) -> None:
    calls = {"reroute": 0, "retime": 0}

    async def fake_reroute(state, scenario_id, event):
        calls["reroute"] += 1
        return None

    async def fake_retime(state, scenario_id):
        calls["retime"] += 1

    monkeypatch.setattr(api, "_reroute_affected_vehicles", fake_reroute)
    monkeypatch.setattr(api, "_apply_disruption_to_live_plan", fake_retime)

    event = _event(DisruptionType.ROAD_CLOSURE)
    result = await api._handle_disruption_effect(object(), "demo", event)
    assert result is None
    assert calls == {"reroute": 1, "retime": 1}


@pytest.mark.asyncio
async def test_non_closure_disruption_only_retimes(monkeypatch) -> None:
    calls = {"reroute": 0, "retime": 0}

    async def fake_reroute(state, scenario_id, event):
        calls["reroute"] += 1
        return _candidate()

    async def fake_retime(state, scenario_id):
        calls["retime"] += 1

    monkeypatch.setattr(api, "_reroute_affected_vehicles", fake_reroute)
    monkeypatch.setattr(api, "_apply_disruption_to_live_plan", fake_retime)

    event = _event(DisruptionType.HEAVY_RAIN)
    result = await api._handle_disruption_effect(object(), "demo", event)
    # Reroute is never attempted for a non-closure; only re-time runs.
    assert result is None
    assert calls == {"reroute": 0, "retime": 1}
