from __future__ import annotations

import numpy as np

from .domain import PlanVersion


def monte_carlo_challenger(
    plan: PlanVersion,
    *,
    samples: int,
    rain_expected: bool,
    seed: int = 401,
) -> dict[str, float]:
    """Deterministic-seed ETA challenger; it informs ranking but never authorizes actions."""
    generator = np.random.default_rng(seed)
    route_minutes = np.array([route.duration_minutes for route in plan.routes], dtype=float)
    sigma = 0.18 if rain_expected else 0.10
    delays = generator.lognormal(
        mean=-0.5 * sigma**2, sigma=sigma, size=(samples, len(route_minutes))
    )
    totals = delays * route_minutes
    fleet_finish = totals.max(axis=1)
    return {
        "p50_finish_minutes": round(float(np.quantile(fleet_finish, 0.50)), 2),
        "p90_finish_minutes": round(float(np.quantile(fleet_finish, 0.90)), 2),
        "overtime_probability": round(float(np.mean(fleet_finish > 600)), 4),
    }
