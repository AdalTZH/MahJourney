from __future__ import annotations

import math
from statistics import median

from .domain import Coordinate, Order, PolicyInput, PolicyTier
from .fixtures import synthetic_fleet, synthetic_orders
from .planning import build_plan, greedy_baseline, plan_duration_under_speeds
from .policy import decide_policy
from .risk import monte_carlo_challenger


def _variant_orders(seed: int) -> tuple[Order, ...]:
    varied = []
    for index, order in enumerate(synthetic_orders()):
        phase = seed * 0.71 + index * 0.37
        location = Coordinate(
            lat=order.location.lat + math.sin(phase) * 0.00035,
            lon=order.location.lon + math.cos(phase) * 0.00035,
        )
        varied.append(
            order.model_copy(
                update={
                    "location": location,
                    "service_seconds": 240 + ((seed + index) % 3) * 60,
                }
            )
        )
    return tuple(varied)


def _urgent_order(seed: int) -> Order:
    return Order(
        order_id=f"URG-{seed:02d}",
        address="Synthetic urgent Jurong delivery",
        location=Coordinate(lat=1.323 + seed * 0.0002, lon=103.684 + seed * 0.0003),
        demand=1,
        window_start_minute=510,
        window_end_minute=900,
        cargo_tags=("URGENT",),
    )


def _scenario_specs() -> tuple[dict, ...]:
    specs = []
    for index in range(8):
        specs.append(
            {
                "name": f"golden-{index + 1:02d}",
                "category": "GOLDEN",
                "event": "NORMAL_DAY",
                "seed": index,
                "policy": PolicyInput(same_driver=True, same_vehicle=True),
                "expected": PolicyTier.AUTO_EXECUTE,
            }
        )
    disruptions = (
        (
            "ROAD_CLOSURE",
            PolicyInput(same_driver=True, same_vehicle=True, eta_degradation_minutes=10),
            PolicyTier.NOTIFY_THEN_EXECUTE,
        ),
        (
            "URGENT_ORDER",
            PolicyInput(same_driver=True, same_vehicle=True, eta_degradation_minutes=4),
            PolicyTier.AUTO_EXECUTE,
        ),
        (
            "TRUCK_BREAKDOWN",
            PolicyInput(same_driver=True, same_vehicle=False),
            PolicyTier.APPROVAL_REQUIRED,
        ),
        (
            "HEAVY_RAIN",
            PolicyInput(same_driver=True, same_vehicle=True, eta_degradation_minutes=12),
            PolicyTier.NOTIFY_THEN_EXECUTE,
        ),
    )
    for index in range(8):
        event, policy, expected = disruptions[index % len(disruptions)]
        specs.append(
            {
                "name": f"disruption-{index + 1:02d}",
                "category": "DISRUPTION",
                "event": event,
                "seed": index + 8,
                "policy": policy,
                "expected": expected,
            }
        )
    adversarial = (
        (
            "STALE_DATA",
            PolicyInput(same_driver=True, same_vehicle=True, data_stale=True),
            PolicyTier.DENY,
        ),
        (
            "INFEASIBLE",
            PolicyInput(same_driver=True, same_vehicle=True, feasible=False),
            PolicyTier.DENY,
        ),
        (
            "HARD_VIOLATION",
            PolicyInput(same_driver=True, same_vehicle=True, hard_violation=True),
            PolicyTier.DENY,
        ),
        (
            "PROTECTED_CARGO",
            PolicyInput(same_driver=True, same_vehicle=True, protected_cargo=True),
            PolicyTier.APPROVAL_REQUIRED,
        ),
        (
            "CROSS_DRIVER",
            PolicyInput(same_driver=False, same_vehicle=True),
            PolicyTier.APPROVAL_REQUIRED,
        ),
        (
            "OVERTIME",
            PolicyInput(same_driver=True, same_vehicle=True, overtime=True),
            PolicyTier.APPROVAL_REQUIRED,
        ),
        (
            "FINAL_STOP_REMOVAL",
            PolicyInput(same_driver=True, same_vehicle=True, final_stop_removed=True),
            PolicyTier.APPROVAL_REQUIRED,
        ),
        (
            "HARD_WINDOW_RELAXATION",
            PolicyInput(same_driver=True, same_vehicle=True, hard_window_relaxed=True),
            PolicyTier.APPROVAL_REQUIRED,
        ),
    )
    for index, (event, policy, expected) in enumerate(adversarial):
        specs.append(
            {
                "name": f"adversarial-{index + 1:02d}",
                "category": "ADVERSARIAL",
                "event": event,
                "seed": index + 16,
                "policy": policy,
                "expected": expected,
            }
        )
    return tuple(specs)


def run_evaluation(samples: int) -> dict:
    results = []
    for spec in _scenario_specs():
        fleet = synthetic_fleet()
        orders = _variant_orders(spec["seed"])
        if spec["event"] == "TRUCK_BREAKDOWN":
            fleet = fleet[:-1]
        if spec["event"] == "URGENT_ORDER":
            orders = (*orders, _urgent_order(spec["seed"]))
        speed_context = {
            order.order_id: (
                7.0 + ((index + spec["seed"]) % 4) * 4
                if spec["category"] == "DISRUPTION" and index % 3 == 0
                else 22.0 + ((index + spec["seed"]) % 5) * 3
            )
            for index, order in enumerate(orders)
        }
        traffic_free = build_plan(fleet, orders, source_data_version=f"{spec['name']}:free")
        candidate = build_plan(
            fleet,
            orders,
            source_data_version=spec["name"],
            speed_kph_by_stop=(speed_context if spec["category"] == "DISRUPTION" else None),
        )
        baseline = greedy_baseline(fleet, orders)
        decision = decide_policy(spec["policy"])
        improvement = (
            (baseline.objective_cost - candidate.objective_cost) / baseline.objective_cost * 100
        )
        unsafe_automatic = decision.tier in (
            PolicyTier.AUTO_EXECUTE,
            PolicyTier.NOTIFY_THEN_EXECUTE,
        ) and (bool(candidate.hard_violations) or not spec["policy"].feasible)
        traffic_free_duration = plan_duration_under_speeds(
            traffic_free, fleet, orders, speed_context
        )
        traffic_aware_duration = plan_duration_under_speeds(candidate, fleet, orders, speed_context)
        traffic_improvement = (
            (traffic_free_duration - traffic_aware_duration) / traffic_free_duration * 100
            if spec["category"] == "DISRUPTION"
            else 0.0
        )
        results.append(
            {
                "scenario": spec["name"],
                "category": spec["category"],
                "event": spec["event"],
                "hard_violations": len(candidate.hard_violations),
                "policy_tier": decision.tier,
                "expected_policy_tier": spec["expected"],
                "policy_compliant": decision.tier == spec["expected"],
                "unsafe_automatic_execution": unsafe_automatic,
                "greedy_cost": baseline.objective_cost,
                "mahjourney_cost": candidate.objective_cost,
                "cost_improvement_percent": round(improvement, 1),
                "traffic_free_duration_minutes": traffic_free_duration,
                "traffic_aware_duration_minutes": traffic_aware_duration,
                "traffic_duration_improvement_percent": round(traffic_improvement, 1),
            }
        )
    hard_compliance = sum(not item["hard_violations"] for item in results) / len(results)
    policy_compliance = sum(item["policy_compliant"] for item in results) / len(results)
    disruption_improvements = [
        item["traffic_duration_improvement_percent"]
        for item in results
        if item["category"] == "DISRUPTION"
    ]
    traffic_median = round(median(disruption_improvements), 1)
    return {
        "scenario_count": len(results),
        "scenario_mix": {"golden": 8, "disruption": 8, "adversarial": 8},
        "hard_constraint_compliance": hard_compliance,
        "policy_compliance": policy_compliance,
        "zero_infeasible_automatic_executions": not any(
            item["unsafe_automatic_execution"] for item in results
        ),
        "median_cost_improvement_percent": round(
            median(item["cost_improvement_percent"] for item in results), 1
        ),
        "traffic_free_ortools_comparison": ("PASS" if traffic_median > 0 else "NEEDS_IMPROVEMENT"),
        "median_disruption_duration_improvement_percent": traffic_median,
        "markov_status": "EXPERIMENTAL",
        "heavy_rain_risk": monte_carlo_challenger(
            build_plan(synthetic_fleet(), synthetic_orders()),
            samples=samples,
            rain_expected=True,
        ),
        "results": results,
    }
