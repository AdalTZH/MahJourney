from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from typing import Any

from .config import get_settings
from .domain import SnapshotStatus
from .integrations import LtaClient, NeaClient, OneMapClient


async def _run_check(name: str, check: Awaitable[Any]) -> dict[str, str]:
    try:
        result = await check
        status = getattr(result.status, "value", str(result.status))
        return {"integration": name, "status": status}
    except Exception as exc:  # Keep independent suites running after failures.
        return {"integration": name, "status": "FETCH_FAILED", "error": type(exc).__name__}


async def run() -> int:
    settings = get_settings()
    if not settings.live_external_read_tests:
        print(json.dumps({"status": "SKIPPED", "reason": "LIVE_EXTERNAL_READ_TESTS=false"}))
        return 0

    onemap = OneMapClient(settings)
    lta = LtaClient(settings)
    nea = NeaClient(settings)
    try:
        results = await asyncio.gather(
            _run_check("OneMap", onemap.health_check()),
            *(
                _run_check(f"LTA:{dataset}", lta.collect(dataset))
                for dataset in LtaClient.DATASETS
            ),
            *(
                _run_check(f"NEA:{dataset}", nea.collect(dataset))
                for dataset in NeaClient.DATASETS
            ),
        )
    finally:
        await onemap.client.aclose()
        await lta.client.aclose()
        await nea.client.aclose()

    print(json.dumps({"checks": results}, sort_keys=True))
    failed = {
        SnapshotStatus.FETCH_FAILED.value,
        SnapshotStatus.AUTHENTICATION_FAILED.value,
    }
    return 1 if any(result["status"] in failed for result in results) else 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
