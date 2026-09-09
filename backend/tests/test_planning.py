from time import perf_counter

from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.planning import build_plan, greedy_baseline, validate_plan


def test_fixture_has_requested_shape() -> None:
    assert len(synthetic_fleet()) == 10
    assert len(synthetic_orders()) == 40


def test_plan_assigns_every_stop_once_without_hard_violations() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    plan = build_plan(fleet, orders)
    assigned = [stop.stop_id for route in plan.routes for stop in route.stops]
    assert len(assigned) == len(set(assigned)) == 40
    assert validate_plan(plan, fleet, orders) == ()
    assert plan.status == "VALIDATED"


def test_candidate_beats_naive_greedy_by_ten_percent() -> None:
    fleet, orders = synthetic_fleet(), synthetic_orders()
    candidate = build_plan(fleet, orders)
    baseline = greedy_baseline(fleet, orders)
    improvement = (baseline.objective_cost - candidate.objective_cost) / baseline.objective_cost
    assert improvement >= 0.10


def test_fixture_plan_completes_within_acceptance_budget() -> None:
    started = perf_counter()
    build_plan(synthetic_fleet(), synthetic_orders())
    assert perf_counter() - started < 10
