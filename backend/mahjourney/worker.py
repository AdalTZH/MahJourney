from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .integrations import LtaClient, NeaClient
from .jobs import PostgresJobCoordinator


async def _repeat(
    name: str,
    integration: str,
    seconds: int,
    action,
    coordinator: PostgresJobCoordinator,
) -> None:
    while True:
        if await coordinator.try_acquire(name, seconds):
            try:
                result = await action()
                if integration == "LTA" and result.dataset == "speed_bands":
                    await coordinator.replace_current_speed_bands(result)
                    await coordinator.compact_speed_band_history()
                await coordinator.record_snapshot(integration, result)
                await coordinator.complete(name, seconds, str(result.status))
                logging.info("collector=%s status=%s", name, result.status)
            except Exception as exc:  # worker isolation: one collector cannot stop the others
                await coordinator.complete(name, seconds, "FETCH_FAILED", type(exc).__name__)
                logging.error("collector=%s error=%s", name, type(exc).__name__)
        await asyncio.sleep(min(5, seconds))


async def main() -> None:
    settings = get_settings()
    lta = LtaClient(settings)
    nea = NeaClient(settings)
    coordinator = PostgresJobCoordinator(settings.database_url)
    intervals = {
        "lta_incidents": settings.lta_incidents_poll_seconds,
        "lta_vms": settings.lta_vms_poll_seconds,
        "lta_speed_bands": settings.lta_speed_bands_poll_seconds,
        "lta_travel_times": settings.lta_travel_times_poll_seconds,
        "nea_rainfall": settings.nea_rainfall_poll_seconds,
        "nea_forecast": settings.nea_two_hour_forecast_poll_seconds,
    }
    await coordinator.ensure(intervals)
    jobs = [
        _repeat(
            "lta_incidents",
            "LTA",
            intervals["lta_incidents"],
            lambda: lta.collect("incidents"),
            coordinator,
        ),
        _repeat("lta_vms", "LTA", intervals["lta_vms"], lambda: lta.collect("vms"), coordinator),
        _repeat(
            "lta_speed_bands",
            "LTA",
            intervals["lta_speed_bands"],
            lambda: lta.collect("speed_bands"),
            coordinator,
        ),
        _repeat(
            "lta_travel_times",
            "LTA",
            intervals["lta_travel_times"],
            lambda: lta.collect("travel_times"),
            coordinator,
        ),
        _repeat(
            "nea_rainfall",
            "NEA",
            intervals["nea_rainfall"],
            lambda: nea.collect("rainfall"),
            coordinator,
        ),
        _repeat(
            "nea_forecast",
            "NEA",
            intervals["nea_forecast"],
            lambda: nea.collect("two_hour_forecast"),
            coordinator,
        ),
    ]
    await asyncio.gather(*jobs)


if __name__ == "__main__":
    asyncio.run(main())
