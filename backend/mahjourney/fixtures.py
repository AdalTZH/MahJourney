from __future__ import annotations

import math

from .domain import Coordinate, Order, Vehicle

DEPOT = Coordinate(lat=1.3214, lon=103.6783)


def synthetic_fleet() -> tuple[Vehicle, ...]:
    return tuple(
        Vehicle(
            vehicle_id=f"TRK-{index:02d}", driver_id=f"DRV-{index:02d}", capacity=6, start=DEPOT
        )
        for index in range(1, 11)
    )


def synthetic_orders() -> tuple[Order, ...]:
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
