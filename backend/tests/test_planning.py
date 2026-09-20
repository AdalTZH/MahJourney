from time import perf_counter

from mahjourney.domain import Coordinate, Order, Vehicle
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


def _single_vehicle_and_far_order() -> tuple[tuple[Vehicle, ...], tuple[Order, ...]]:
    """A vehicle that can reach a distant order well before its own window.

    With windows enforced, the vehicle must wait until window_start_minute.
    With windows disabled, it should serve the order as soon as it arrives.
    """
    depot = Coordinate(lat=1.30, lon=103.80)
    vehicle = Vehicle(
        vehicle_id="TRK-01",
        driver_id="DRV-01",
        start=depot,
        working_start_minute=480,
        working_end_minute=1080,
    )
    order = Order(
        order_id="ORD-01",
        address="Far stop",
        location=Coordinate(lat=1.30, lon=103.81),
        window_start_minute=900,
        window_end_minute=960,
    )
    return (vehicle,), (order,)


def test_delivery_window_enforced_by_default_waits_for_window_start() -> None:
    vehicles, orders = _single_vehicle_and_far_order()
    plan = build_plan(vehicles, orders)
    stop = plan.routes[0].stops[0]
    assert stop.eta_minute == orders[0].window_start_minute
    assert validate_plan(plan, vehicles, orders) == ()


def test_delivery_window_disabled_serves_as_soon_as_vehicle_arrives() -> None:
    vehicles, orders = _single_vehicle_and_far_order()
    plan = build_plan(vehicles, orders, enforce_delivery_windows=False)
    stop = plan.routes[0].stops[0]
    # Served on arrival, well before the order's own (now-ignored) window.
    assert stop.eta_minute < orders[0].window_start_minute
    assert stop.eta_minute >= vehicles[0].working_start_minute
    assert validate_plan(plan, vehicles, orders, enforce_delivery_windows=False) == ()
    assert plan.status == "VALIDATED"
    assert plan.hard_violations == ()


def test_delivery_window_disabled_still_flags_working_hours_overrun() -> None:
    """Disabling windows must not silently hide a working-hours violation.

    With no depots (the legacy single-origin path used here), an infeasible
    order is still served rather than dropped, matching existing behavior for
    windowed planning too — but it must still surface as a violation so a
    plan that overruns a driver's shift is never reported as VALIDATED.
    """
    depot = Coordinate(lat=1.30, lon=103.80)
    vehicle = Vehicle(
        vehicle_id="TRK-01",
        driver_id="DRV-01",
        start=depot,
        working_start_minute=480,
        working_end_minute=481,  # a one-minute shift: far too short for the trip
    )
    far_order = Order(
        order_id="ORD-01",
        address="Too far for the shift",
        location=Coordinate(lat=1.50, lon=104.00),
        window_start_minute=0,
        window_end_minute=1439,
    )
    plan = build_plan((vehicle,), (far_order,), enforce_delivery_windows=False)
    violations = validate_plan(
        plan, (vehicle,), (far_order,), enforce_delivery_windows=False
    )
    assert any("past driver working hours" in violation for violation in violations)
    assert plan.status == "CANDIDATE"
