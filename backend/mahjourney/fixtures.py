"""Synthetic fleet/orders for the OFFLINE evaluation harness and tests ONLY.

The running application MUST NEVER serve synthetic data: operational data
(fleet, orders, depots) is sourced exclusively from PostgreSQL via
``AppState.initialize`` (which raises if the DB is empty). To make that
guarantee enforceable rather than a convention, the generators below refuse to
run unless the caller is the evaluation harness or a test. Any attempt to pull
fake data into a request/runtime path fails loudly here instead of silently
returning fixtures.
"""

from __future__ import annotations

import math
import sys
import traceback

from .domain import Coordinate, Order, Vehicle

DEPOT = Coordinate(lat=1.3214, lon=103.6783)

# Modules permitted to produce synthetic data: the offline evaluation harness,
# the offline OR-Tools tuning sweep, and the test suite. Anything else (i.e. the
# live application) is forbidden.
_ALLOWED_CALLER_SUFFIXES = ("mahjourney.evaluation", "mahjourney.tune_ortools")


def _assert_non_runtime_caller() -> None:
    """Fail loudly if synthetic data is requested from a runtime code path.

    Allowed only when (a) running under pytest, or (b) called from the
    evaluation harness. This turns "the app never uses fake data" from a
    convention into an enforced invariant: a stray fixture call in the request
    path raises instead of silently poisoning results with fake fleet/orders.
    """
    if "pytest" in sys.modules:
        return
    stack = traceback.extract_stack()
    # Walk callers (excluding this frame) looking for an allowed module.
    for frame in stack[:-1]:
        module_globals_name = frame.filename.replace("\\", "/")
        if any(
            module_globals_name.endswith(f"{suffix.split('.')[-1]}.py")
            for suffix in _ALLOWED_CALLER_SUFFIXES
        ):
            return
    raise RuntimeError(
        "synthetic_fleet/synthetic_orders are for the evaluation harness and "
        "tests only. The application sources fleet/orders exclusively from the "
        "database (see AppState.initialize). Do not use fixtures at runtime."
    )


def synthetic_fleet() -> tuple[Vehicle, ...]:
    _assert_non_runtime_caller()
    return tuple(
        Vehicle(
            vehicle_id=f"TRK-{index:02d}", driver_id=f"DRV-{index:02d}", capacity=6, start=DEPOT
        )
        for index in range(1, 11)
    )


def synthetic_orders() -> tuple[Order, ...]:
    _assert_non_runtime_caller()
    centers = (
        (1.331, 103.704),
        (1.343, 103.722),
        (1.315, 103.696),
        (1.304, 103.714),
        (1.327, 103.746),
        (1.349, 103.754),
        (1.296, 103.742),
        (1.312, 103.768),
        (1.338, 103.782),
        (1.288, 103.781),
    )
    orders: list[Order] = []
    for index in range(40):
        cluster = index % len(centers)
        ring = index // len(centers)
        base_lat, base_lon = centers[cluster]
        angle = cluster * 0.63 + ring * 1.27
        location = Coordinate(
            lat=base_lat + math.sin(angle) * 0.0025,
            lon=base_lon + math.cos(angle) * 0.003,
        )
        orders.append(
            Order(
                order_id=f"ORD-{index + 1:03d}",
                address=f"Synthetic Jurong stop {index + 1}",
                location=location,
                demand=1,
                window_start_minute=480 + (cluster % 3) * 30,
                window_end_minute=1020,
            )
        )
    return tuple(orders)
