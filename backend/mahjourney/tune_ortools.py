"""Sweep the OR-Tools search budget and report the cost/latency tradeoff.

Route quality improves as OR-Tools' GUIDED_LOCAL_SEARCH is given more wall-clock
time, but only up to a point — past some budget the objective cost stops
dropping while plan latency keeps climbing. This tool builds a full plan at each
candidate ``ortools_time_limit_seconds`` and prints, per setting:

* ``cost``            — plan objective cost (total distance, km)
* ``vs_greedy``       — percent better than the naive greedy baseline
* ``vs_best``         — percent worse than the cheapest plan found in the sweep
* ``dropped``         — orders left unassigned (see below) — 0 is required
* ``latency_s``       — wall-clock time to build the plan
* ``routes``          — number of vehicle routes in the plan

Read the table top-to-bottom. ``dropped`` takes priority over cost: a low
budget can leave a tight order unassigned (the OR-Tools search runs out of time
before it can fit it), which shows up here as a non-zero count and as an
``unassigned stop`` hard violation on the plan. Never pick a budget with
``dropped`` > 0 no matter how good its cost looks — a cheaper plan that skips an
order is not actually cheaper. Among the budgets with ``dropped`` == 0, pick the
smallest where ``cost`` has plateaued (``vs_best`` near 0%). Paying for more time
past the plateau just adds latency.

Usage (from the backend directory)::

    # Against your real operational data in PostgreSQL (recommended — tunes to
    # your actual depot sizes and order counts):
    python -m mahjourney.tune_ortools

    # Custom budget grid:
    python -m mahjourney.tune_ortools --limits 0.5 1 2 3 5 8

    # DB-less smoke test on synthetic fixtures (small; not representative of
    # real depot sizes, only useful to confirm the tool runs):
    python -m mahjourney.tune_ortools --synthetic

    # Diagnose dropped orders: split them into genuinely infeasible (no
    # same-depot vehicle can serve them alone) vs. solver/budget drops:
    python -m mahjourney.tune_ortools --feasibility-check

    # Per-depot demand vs. hard capacity ceilings (stops/weight/volume):
    python -m mahjourney.tune_ortools --capacity-report
"""

from __future__ import annotations

import argparse
import asyncio
from time import perf_counter

from .config import get_settings
from .domain import Depot, Order, Vehicle
from .planning import (
    assign_orders_to_depots,
    build_plan,
    depot_node_id,
    effective_window,
    greedy_baseline,
    leg_travel_minutes,
    order_service_seconds,
)

DEFAULT_LIMITS = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0)


def _order_feasible_for_vehicle(
    order: Order, vehicle: Vehicle, origin, *, enforce_delivery_windows: bool
) -> bool:
    """Whether one vehicle could serve ``order`` ALONE, out and back from depot.

    Ground-truth feasibility of a single order, independent of any solver: the
    round trip depot -> order -> depot plus service must fit inside both the
    order's effective window and the vehicle's shift, and the order's weight and
    volume must fit the vehicle's capacity. This deliberately ignores every
    OTHER order, so it isolates "this order can never be served by this vehicle"
    (a hard data/fleet property) from "the solver couldn't fit it alongside the
    rest in the time it had" (a solver/budget property).
    """
    if order.weight_kg > vehicle.capacity_weight_kg + 1e-6:
        return False
    if order.volume_m3 > vehicle.capacity_volume_m3 + 1e-6:
        return False
    depot_id = depot_node_id(origin)
    out = leg_travel_minutes(depot_id, order.order_id, origin, order.location, 28.0, None)
    window_start, window_end = effective_window(
        order, vehicle, enforce_delivery_windows=enforce_delivery_windows
    )
    # Earliest the vehicle can begin service: leave at shift start, but not
    # before the window opens.
    arrival = vehicle.working_start_minute + out
    service_start = max(arrival, window_start)
    if service_start > window_end:
        return False
    service_end = service_start + (order_service_seconds(order) + 59) // 60
    back = leg_travel_minutes(order.order_id, depot_id, order.location, origin, 28.0, None)
    return service_end + back <= vehicle.working_end_minute


def _feasible_order_ids(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    depots: tuple[Depot, ...],
    *,
    enforce_delivery_windows: bool,
) -> tuple[frozenset[str], dict[str, list[Order]]]:
    """Ground-truth feasibility per order, evaluated against its DEPOT's fleet.

    Mirrors how build_plan assigns orders to depots, then tests each order only
    against the vehicles based at that depot (the ones that could actually serve
    it). Returns the set of order ids that at least one same-depot vehicle can
    serve alone, plus the per-depot vehicle grouping (reused by the caller).
    """
    assigned, _ = assign_orders_to_depots(orders, depots)
    vehicles_by_depot: dict[str, list[Vehicle]] = {}
    for vehicle in vehicles:
        vehicles_by_depot.setdefault(vehicle.depot_id, []).append(vehicle)
    depot_origin = {depot.depot_id: depot.location for depot in depots}
    feasible: set[str] = set()
    for order in assigned:
        depot_vehicles = vehicles_by_depot.get(order.assigned_depot_id, [])
        origin = depot_origin.get(
            order.assigned_depot_id,
            depot_vehicles[0].start if depot_vehicles else order.location,
        )
        if any(
            _order_feasible_for_vehicle(
                order, vehicle, origin, enforce_delivery_windows=enforce_delivery_windows
            )
            for vehicle in depot_vehicles
        ):
            feasible.add(order.order_id)
    return frozenset(feasible), vehicles_by_depot


def capacity_report(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    depots: tuple[Depot, ...],
    max_stops: int,
    *,
    enforce_delivery_windows: bool,
) -> None:
    """Per-depot: feasible demand vs. the three hard capacity ceilings.

    For each depot compares the demand actually routable to it (its feasible
    orders — those at least one same-depot vehicle can serve alone) against:

    * stops  : feasible order count      vs. vehicles_at_depot * max_stops
    * weight : sum(order.weight_kg)       vs. sum(vehicle.capacity_weight_kg)
    * volume : sum(order.volume_m3)       vs. sum(vehicle.capacity_volume_m3)

    Any dimension where demand exceeds the ceiling is flagged with the overage,
    because that overflow MUST be dropped by any solver regardless of budget —
    it is a fleet/config ceiling, not a search failure. This is what tells
    apart "raise max_stops_per_vehicle" (stops over, weight/volume slack) from
    "add vehicles / fleet undersized" (weight or volume over).
    """
    feasible_ids, vehicles_by_depot = _feasible_order_ids(
        vehicles, orders, depots, enforce_delivery_windows=enforce_delivery_windows
    )
    assigned, _ = assign_orders_to_depots(orders, depots)
    orders_by_depot: dict[str, list[Order]] = {}
    for order in assigned:
        if order.order_id in feasible_ids:
            orders_by_depot.setdefault(order.assigned_depot_id, []).append(order)
    depot_name = {depot.depot_id: (depot.name or depot.depot_id) for depot in depots}

    print(
        f"\nPer-depot capacity vs. feasible demand (max_stops_per_vehicle="
        f"{max_stops})\n"
    )
    header = (
        f"{'depot':>16}  {'veh':>3}  {'stops_dem':>9}/{'cap':<5}  "
        f"{'wt_dem':>8}/{'wt_cap':<8}  {'vol_dem':>8}/{'vol_cap':<8}  over"
    )
    print(header)
    print("-" * len(header))
    over_any = 0
    for depot in depots:
        did = depot.depot_id
        depot_vehicles = vehicles_by_depot.get(did, [])
        depot_orders = orders_by_depot.get(did, [])
        n_veh = len(depot_vehicles)
        stops_cap = n_veh * max_stops
        stops_dem = len(depot_orders)
        wt_dem = sum(o.weight_kg for o in depot_orders)
        wt_cap = sum(v.capacity_weight_kg for v in depot_vehicles)
        vol_dem = sum(o.volume_m3 for o in depot_orders)
        vol_cap = sum(v.capacity_volume_m3 for v in depot_vehicles)
        overs = []
        if stops_dem > stops_cap:
            overs.append(f"stops+{stops_dem - stops_cap}")
        if wt_dem > wt_cap + 1e-6:
            overs.append(f"wt+{wt_dem - wt_cap:.0f}kg")
        if vol_dem > vol_cap + 1e-6:
            overs.append(f"vol+{vol_dem - vol_cap:.2f}m3")
        if overs:
            over_any += 1
        label = depot_name[did][:16]
        print(
            f"{label:>16}  {n_veh:>3}  {stops_dem:>9}/{stops_cap:<5}  "
            f"{wt_dem:>8.0f}/{wt_cap:<8.0f}  {vol_dem:>8.2f}/{vol_cap:<8.2f}  "
            f"{','.join(overs) if overs else '-'}"
        )
    print()
    if over_any:
        print(
            f"{over_any} depot(s) over capacity on at least one dimension. The "
            "overage on each flagged dimension is demand that MUST be dropped by "
            "any solver at any budget — resolve it (raise max_stops, add vehicles, "
            "or accept the fleet can't serve this demand)."
        )
    else:
        print(
            "No depot is over capacity on stops/weight/volume. If a solver still "
            "drops feasible orders, the cause is search budget or model shape, "
            "not a hard fleet ceiling."
        )
    print()


def feasibility_check(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    depots: tuple[Depot, ...],
    dropped_ids: frozenset[str],
    *,
    enforce_delivery_windows: bool,
) -> None:
    """Report, for the dropped orders, which are genuinely infeasible.

    Splits the dropped set into (a) orders no same-depot vehicle could serve
    even alone — genuine, data/fleet infeasibility that no solver or budget can
    fix — and (b) orders that ARE servable in isolation but were still dropped —
    the solver gave up under load/budget. Only the second bucket is a solver
    problem worth chasing.
    """
    feasible_ids, _ = _feasible_order_ids(
        vehicles, orders, depots, enforce_delivery_windows=enforce_delivery_windows
    )
    genuine = sorted(oid for oid in dropped_ids if oid not in feasible_ids)
    solver_dropped = sorted(oid for oid in dropped_ids if oid in feasible_ids)
    total_infeasible = sum(1 for o in orders if o.order_id not in feasible_ids)
    print(
        f"\nFeasibility of dropped orders — {len(dropped_ids)} dropped, evaluated "
        "against each order's own depot fleet (out-and-back in isolation):\n"
    )
    print(
        f"  genuinely infeasible (no same-depot vehicle can serve alone): "
        f"{len(genuine)}"
    )
    print(f"  servable alone but still dropped (solver/budget): {len(solver_dropped)}")
    print(
        f"  (whole dataset: {total_infeasible}/{len(orders)} orders are infeasible "
        "for every same-depot vehicle)\n"
    )
    if genuine:
        print(f"  genuinely infeasible ids: {genuine}")
    if solver_dropped:
        print(f"  solver-dropped-despite-feasible ids: {solver_dropped}")
    print()


async def _load_from_db(
    database_url: str,
) -> tuple[tuple[Vehicle, ...], tuple[Order, ...], tuple[Depot, ...]]:
    """Load the same fleet/orders/depots the running app plans against."""
    from .repository import PostgresRepository

    repo = PostgresRepository(database_url)
    try:
        await repo.check()
        depots = await repo.load_depots()
        fleet = await repo.load_fleet(available_only=True)
        orders = await repo.load_orders(pending_only=True)
    finally:
        await repo.engine.dispose()
    if not fleet or not orders:
        raise SystemExit(
            "The database has no usable fleet/orders. Import operational data first:\n"
            "  python -m mahjourney.import_operational --workbook <path>\n"
            "or run the sweep on synthetic fixtures with --synthetic."
        )
    return fleet, orders, depots


def _load_synthetic() -> tuple[tuple[Vehicle, ...], tuple[Order, ...], tuple[Depot, ...]]:
    # Allowed by fixtures._assert_non_runtime_caller because this module is
    # whitelisted there as an offline analysis tool.
    from .fixtures import synthetic_fleet, synthetic_orders

    return synthetic_fleet(), synthetic_orders(), ()


def run_sweep(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    depots: tuple[Depot, ...],
    limits: tuple[float, ...],
) -> list[dict[str, float | int]]:
    """Build a plan at each time limit and collect cost + latency per setting."""
    baseline_cost = greedy_baseline(vehicles, orders).objective_cost
    rows: list[dict[str, float | int]] = []
    for limit in limits:
        started = perf_counter()
        plan = build_plan(
            vehicles,
            orders,
            depots=depots,
            max_stops_per_vehicle=get_settings().max_stops_per_vehicle,
            ortools_time_limit_seconds=limit,
        )
        latency = perf_counter() - started
        # Orders the solver couldn't fit surface as "unassigned stop <id>"
        # hard violations (see validate_plan / the AddDisjunction drop penalty
        # in planning.py). Count them so a budget that silently sheds an order
        # is visible next to its cost.
        dropped = sum(1 for v in plan.hard_violations if v.startswith("unassigned stop"))
        rows.append(
            {
                "limit": limit,
                "cost": plan.objective_cost,
                "dropped": dropped,
                "latency": latency,
                "routes": len(plan.routes),
            }
        )
    best_cost = min((row["cost"] for row in rows), default=0.0) or 1.0
    for row in rows:
        vs_greedy = (
            (baseline_cost - row["cost"]) / baseline_cost * 100.0 if baseline_cost > 0 else 0.0
        )
        # How much worse than the cheapest plan found this sweep; ~0% means the
        # cost has plateaued at this budget.
        vs_best = (row["cost"] - best_cost) / best_cost * 100.0
        row["vs_greedy"] = vs_greedy
        row["vs_best"] = vs_best
    return rows


def _print_table(
    rows: list[dict[str, float | int]],
    *,
    fleet_size: int,
    order_count: int,
    depot_count: int,
    baseline_cost: float,
) -> None:
    print(
        f"\nOR-Tools time-limit sweep — {order_count} orders, {fleet_size} vehicles, "
        f"{depot_count} depots (greedy baseline cost={baseline_cost:.2f})\n"
    )
    header = (
        f"{'limit_s':>8}  {'cost':>10}  {'vs_greedy':>10}  {'vs_best':>9}  "
        f"{'dropped':>7}  {'latency_s':>10}  {'routes':>6}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        # Flag any budget that dropped an order so it stands out in the table.
        flag = "  <-- DROPS ORDERS" if row["dropped"] else ""
        print(
            f"{row['limit']:>8.2f}  {row['cost']:>10.2f}  {row['vs_greedy']:>9.1f}%  "
            f"{row['vs_best']:>8.1f}%  {int(row['dropped']):>7}  {row['latency']:>10.2f}  "
            f"{int(row['routes']):>6}{flag}"
        )
    safe = [row for row in rows if not row["dropped"]]
    if not safe:
        print(
            "\nWARNING: every budget in this sweep dropped at least one order. "
            "Try larger --limits values.\n"
        )
    else:
        print(
            "\nPick the smallest limit with dropped=0 where vs_best is ~0% (cost has "
            "plateaued) and set ORTOOLS_TIME_LIMIT_SECONDS to it. Never pick a budget "
            "that drops orders, however good its cost looks.\n"
        )


def _two_stage_plan(
    vehicles: tuple[Vehicle, ...],
    orders: tuple[Order, ...],
    depots: tuple[Depot, ...],
    *,
    limit: float,
) -> dict[str, float | int]:
    """Build one plan and collect cost, dropped order ids, and latency.

    ``limit`` is the plan-wide OR-Tools pool (``ortools_time_limit_seconds``),
    split across per-vehicle solves internally. Used by --feasibility-check to
    obtain the set of orders a normal build drops.
    """
    started = perf_counter()
    plan = build_plan(
        vehicles,
        orders,
        depots=depots,
        max_stops_per_vehicle=get_settings().max_stops_per_vehicle,
        ortools_time_limit_seconds=limit,
    )
    latency = perf_counter() - started
    # Dropped orders surface as "unassigned stop <id>" hard violations. Capture
    # the ids so downstream analysis can tell which orders went unserved.
    dropped_ids = frozenset(
        v.removeprefix("unassigned stop ").strip()
        for v in plan.hard_violations
        if v.startswith("unassigned stop")
    )
    return {
        "cost": plan.objective_cost,
        "dropped": len(dropped_ids),
        "dropped_ids": dropped_ids,
        "latency": latency,
        "routes": len(plan.routes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep ortools_time_limit_seconds and report plan cost vs. latency."
    )
    parser.add_argument(
        "--limits",
        type=float,
        nargs="+",
        default=list(DEFAULT_LIMITS),
        help=f"Time-limit values (seconds) to try. Default: {' '.join(map(str, DEFAULT_LIMITS))}",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "Run against synthetic fixtures instead of the database. Small and "
            "not representative of real depot sizes; use only to smoke-test the "
            "tool without a DB."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override the async SQLAlchemy database URL (defaults to settings).",
    )
    parser.add_argument(
        "--feasibility-check",
        action="store_true",
        help=(
            "Build a plan, then for every dropped order report whether any "
            "same-depot vehicle could serve it ALONE (out-and-back within window "
            "and shift, fits capacity). Separates genuine data/fleet "
            "infeasibility from solver/budget drops. Uses --limit as the plan "
            "budget (defaults to ORTOOLS_TIME_LIMIT_SECONDS). Needs real DB data "
            "with depots."
        ),
    )
    parser.add_argument(
        "--limit",
        type=float,
        default=None,
        help=(
            "Plan-wide OR-Tools budget (seconds) for --feasibility-check "
            "(split across vehicles). Defaults to ORTOOLS_TIME_LIMIT_SECONDS."
        ),
    )
    parser.add_argument(
        "--capacity-report",
        action="store_true",
        help=(
            "Per depot, compare feasible demand against the three hard ceilings "
            "(stops = vehicles x max_stops, total weight, total volume) and flag "
            "any depot over capacity on any dimension. No solve — pure "
            "fleet-vs-demand accounting. Needs real DB data with depots."
        ),
    )
    args = parser.parse_args()
    limits = tuple(sorted(set(args.limits)))

    if args.synthetic:
        vehicles, orders, depots = _load_synthetic()
    else:
        database_url = args.database_url or get_settings().database_url
        vehicles, orders, depots = asyncio.run(_load_from_db(database_url))

    if args.capacity_report:
        if not depots:
            raise SystemExit(
                "--capacity-report needs depots (per-depot fleet). Run against "
                "real DB data, not --synthetic."
            )
        settings = get_settings()
        capacity_report(
            vehicles,
            orders,
            depots,
            settings.max_stops_per_vehicle,
            enforce_delivery_windows=settings.enforce_delivery_windows,
        )
        return

    if args.feasibility_check:
        if not depots:
            raise SystemExit(
                "--feasibility-check needs depots (per-depot fleet). Run against "
                "real DB data, not --synthetic."
            )
        settings = get_settings()
        pool = (
            args.limit
            if args.limit is not None
            else float(settings.ortools_time_limit_seconds)
        )
        result = _two_stage_plan(vehicles, orders, depots, limit=pool)
        print(
            f"\nPlan at {pool:.2f}s pool: {result['dropped']} dropped, "
            f"cost={result['cost']:.2f}, latency={result['latency']:.2f}s"
        )
        feasibility_check(
            vehicles,
            orders,
            depots,
            result["dropped_ids"],
            enforce_delivery_windows=settings.enforce_delivery_windows,
        )
        return

    baseline_cost = greedy_baseline(vehicles, orders).objective_cost
    rows = run_sweep(vehicles, orders, depots, limits)
    _print_table(
        rows,
        fleet_size=len(vehicles),
        order_count=len(orders),
        depot_count=len(depots),
        baseline_cost=baseline_cost,
    )


if __name__ == "__main__":
    main()
