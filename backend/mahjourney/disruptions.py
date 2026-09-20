"""Turn active disruption events into real travel-speed penalties.

Only :class:`~mahjourney.domain.DisruptionType` values that are genuine planning
factors are handled here: ``ROAD_CLOSURE`` and ``HEAVY_RAIN``. ``TRUCK_BREAKDOWN``
and ``URGENT_ORDER`` are scenario-lab concepts, not planning inputs, and are
intentionally ignored — including them would require deciding how to reassign a
disrupted vehicle's stops, which is a separate, larger change.

The model is deliberately simple and explainable rather than a full traffic
simulation: a road closure slows any order within a radius of the closure point
(worse near the center, tapering to no effect at the radius edge, approximating
the detour a driver must take); heavy rain applies a uniform fleet-wide slowdown.
Effects compound multiplicatively when both apply to the same stop. This produces
a real, deterministic added-time figure per order rather than a guess.
"""

from __future__ import annotations

from .domain import Coordinate, DisruptionEvent, DisruptionType, Order
from .planning import haversine_km

BASE_SPEED_KPH = 28.0

# A closure forces a detour; speed drops most at the closure's center and
# tapers linearly to no effect at the edge of the affected radius.
CLOSURE_CENTER_SPEED_FACTOR = 0.4
DEFAULT_CLOSURE_RADIUS_KM = 1.5

# Uniform slowdown applied fleet-wide while heavy rain is active.
RAIN_SEVERITY_SPEED_FACTOR = {"MODERATE": 0.88, "HEAVY": 0.75}
DEFAULT_RAIN_SEVERITY = "HEAVY"

# Disruption types that are real planning inputs. TRUCK_BREAKDOWN and
# URGENT_ORDER are deliberately excluded (see module docstring).
PLANNING_DISRUPTION_TYPES = (DisruptionType.ROAD_CLOSURE, DisruptionType.HEAVY_RAIN)


def active_disruptions(
    events: tuple[DisruptionEvent, ...], current_minute: int
) -> tuple[DisruptionEvent, ...]:
    """Planning-relevant disruptions whose effective time has already passed.

    There is no "cleared" state yet, so an event is considered active from its
    effective minute onward for the current scenario day.
    """
    return tuple(
        event
        for event in events
        if event.event_type in PLANNING_DISRUPTION_TYPES
        and event.effective_minute <= current_minute
    )


def _closure_speed_factor(distance_km: float, radius_km: float) -> float:
    if distance_km >= radius_km:
        return 1.0
    # Linear taper from the center penalty up to no penalty at the radius edge.
    proportion = distance_km / radius_km if radius_km > 0 else 0.0
    return CLOSURE_CENTER_SPEED_FACTOR + (1.0 - CLOSURE_CENTER_SPEED_FACTOR) * proportion


def speed_factor_for_location(
    location: Coordinate, disruptions: tuple[DisruptionEvent, ...]
) -> float:
    """Combined [0, 1] speed multiplier at a location from all active disruptions."""
    factor = 1.0
    for event in disruptions:
        if event.event_type == DisruptionType.ROAD_CLOSURE:
            lat = event.payload.get("lat")
            lon = event.payload.get("lon")
            if lat is None or lon is None:
                continue
            radius_km = float(event.payload.get("radius_km", DEFAULT_CLOSURE_RADIUS_KM))
            distance = haversine_km(location, Coordinate(lat=float(lat), lon=float(lon)))
            factor *= _closure_speed_factor(distance, radius_km)
        elif event.event_type == DisruptionType.HEAVY_RAIN:
            severity = str(event.payload.get("severity", DEFAULT_RAIN_SEVERITY)).upper()
            factor *= RAIN_SEVERITY_SPEED_FACTOR.get(severity, RAIN_SEVERITY_SPEED_FACTOR["HEAVY"])
    return factor


def disruption_speed_kph_by_stop(
    orders: tuple[Order, ...],
    disruptions: tuple[DisruptionEvent, ...],
    base_speed_kph_by_stop: dict[str, float] | None = None,
) -> dict[str, float] | None:
    """Per-order travel speed reflecting active disruptions.

    Starts from ``base_speed_kph_by_stop`` (for example live LTA speed-band
    context) when supplied, so disruption penalties compound on top of already
    observed traffic conditions rather than replacing them. Returns ``None``
    when there are no active disruptions and no base context, matching the
    "no traffic context" default used elsewhere in planning.
    """
    if not disruptions:
        return base_speed_kph_by_stop
    speeds: dict[str, float] = {}
    for order in orders:
        base = (base_speed_kph_by_stop or {}).get(order.order_id, BASE_SPEED_KPH)
        speeds[order.order_id] = max(
            5.0, base * speed_factor_for_location(order.location, disruptions)
        )
    return speeds
