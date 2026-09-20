from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings
from .domain import IntegrationHealth, SnapshotStatus, TrafficSnapshot, WeatherSnapshot


def _hash_payload(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def token_expiry(token: str) -> datetime | None:
    try:
        payload_part = token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part))
        return datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def expiry_message(expiry: datetime | None) -> str:
    if expiry is None:
        return ""
    remaining = (expiry - datetime.now(UTC)).total_seconds() / 86400
    if remaining <= 0:
        return "token expiry claim is in the past; API response remains authoritative"
    for threshold in (1, 7, 14):
        if remaining <= threshold:
            return f"token expiry warning: fewer than {threshold} day(s) remain"
    return ""


class OneMapClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10)
        self.authentication_failed = False
        self.search_cache: dict[str, dict[str, Any]] = {}
        self.route_cache: dict[str, dict[str, Any]] = {}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.authentication_failed:
            raise RuntimeError("OneMap disabled after authentication failure")
        for attempt in range(3):
            try:
                response = await self.client.get(
                    f"{self.settings.onemap_base_url.rstrip('/')}{path}",
                    params=params,
                    headers={"Authorization": self.settings.onemap_access_token},
                )
            except httpx.RequestError:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (2**attempt))
                    continue
                raise
            payload = response.json()
            if response.status_code == 401 or (
                isinstance(payload, dict) and payload.get("error")
            ):
                self.authentication_failed = True
                raise PermissionError("OneMap rejected the configured access token")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (2**attempt))
                    continue
            response.raise_for_status()
            return payload
        raise RuntimeError("OneMap transient retry budget exhausted")

    async def search(self, query: str) -> dict[str, Any]:
        key = " ".join(query.upper().split())
        if key not in self.search_cache:
            self.search_cache[key] = await self._get(
                "/api/common/elastic/search",
                {"searchVal": query, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1},
            )
        return self.search_cache[key]

    async def route(self, start: tuple[float, float], end: tuple[float, float]) -> dict[str, Any]:
        key = f"{start[0]:.6f},{start[1]:.6f}:{end[0]:.6f},{end[1]:.6f}:drive"
        if key not in self.route_cache:
            self.route_cache[key] = await self._get(
                "/api/public/routingsvc/route",
                {
                    "start": f"{start[0]},{start[1]}",
                    "end": f"{end[0]},{end[1]}",
                    "routeType": "drive",
                },
            )
        return self.route_cache[key]

    async def health_check(self) -> IntegrationHealth:
        if not self.settings.onemap_access_token:
            return IntegrationHealth(
                integration="OneMap",
                status=SnapshotStatus.NOT_CONFIGURED,
                message="ONEMAP_ACCESS_TOKEN is blank",
            )
        started = time.perf_counter()
        try:
            await self.search("619495")
            await self.route((1.3214, 103.6783), (1.3290, 103.7060))
        except PermissionError as exc:
            return IntegrationHealth(
                integration="OneMap",
                status=SnapshotStatus.AUTHENTICATION_FAILED,
                latency_ms=int((time.perf_counter() - started) * 1000),
                message=str(exc),
                expires_at=token_expiry(self.settings.onemap_access_token),
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return IntegrationHealth(
                integration="OneMap",
                status=SnapshotStatus.FETCH_FAILED,
                latency_ms=int((time.perf_counter() - started) * 1000),
                message=f"read-only health check failed: {type(exc).__name__}",
                expires_at=token_expiry(self.settings.onemap_access_token),
            )
        expiry = token_expiry(self.settings.onemap_access_token)
        return IntegrationHealth(
            integration="OneMap",
            status=SnapshotStatus.FRESH,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message="; ".join(
                value
                for value in ("Search and Routing read-only checks passed", expiry_message(expiry))
                if value
            ),
            expires_at=expiry,
        )

    def reset_authentication_failure(self) -> None:
        self.authentication_failed = False


class LtaClient:
    BASE_URL = "https://datamall2.mytransport.sg/ltaodataservice"
    DATASETS = {
        "incidents": "TrafficIncidents",
        "vms": "VMS",
        "speed_bands": "v4/TrafficSpeedBands",
        "travel_times": "EstTravelTimes",
    }

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=15)
        self.last_hashes: dict[str, str] = {}
        self.last_valid: dict[str, TrafficSnapshot] = {}
        self._locks = {dataset: asyncio.Lock() for dataset in self.DATASETS}

    async def collect(self, dataset: str) -> TrafficSnapshot:
        if not self.settings.lta_datamall_account_key:
            return TrafficSnapshot(
                dataset=dataset,
                response_hash="",
                record_count=0,
                status=SnapshotStatus.NOT_CONFIGURED,
            )
        async with self._locks[dataset]:
            records: list[dict[str, Any]] = []
            skip = 0
            for attempt in range(3):
                try:
                    while True:
                        response = await self.client.get(
                            f"{self.BASE_URL}/{self.DATASETS[dataset]}",
                            params={"$skip": skip},
                            headers={
                                "AccountKey": self.settings.lta_datamall_account_key,
                                "accept": "application/json",
                            },
                        )
                        response.raise_for_status()
                        page = response.json().get("value", [])
                        if not isinstance(page, list):
                            raise ValueError("LTA response value must be a list")
                        records.extend(page)
                        if len(page) < 500:
                            break
                        skip += 500
                    break
                except (httpx.HTTPError, ValueError):
                    if attempt == 2:
                        prior = self.last_valid.get(dataset)
                        if prior:
                            return prior.model_copy(update={"status": SnapshotStatus.FETCH_FAILED})
                        return TrafficSnapshot(
                            dataset=dataset,
                            response_hash="",
                            record_count=0,
                            status=SnapshotStatus.FETCH_FAILED,
                        )
                    await asyncio.sleep(0.25 * (2**attempt))
            digest = _hash_payload(records)
            snapshot = TrafficSnapshot(
                dataset=dataset,
                response_hash=digest,
                record_count=len(records),
                status=SnapshotStatus.FRESH,
                records=tuple(records),
            )
            self.last_hashes[dataset] = digest
            self.last_valid[dataset] = snapshot
            return snapshot


class NeaClient:
    DATASETS = {"rainfall": "/rainfall", "two_hour_forecast": "/two-hr-forecast"}

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=15)
        self.last_valid: dict[str, WeatherSnapshot] = {}

    async def collect(self, dataset: str) -> WeatherSnapshot:
        headers = (
            {"x-api-key": self.settings.data_gov_sg_api_key}
            if self.settings.data_gov_sg_api_key
            else {}
        )
        try:
            response = await self.client.get(
                f"{self.settings.nea_weather_base_url.rstrip('/')}{self.DATASETS[dataset]}",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            records = payload.get("data", {}).get(
                "readings", payload.get("data", {}).get("items", [])
            )
            if isinstance(records, dict):
                records = [records]
            snapshot = WeatherSnapshot(
                dataset=dataset,
                response_hash=_hash_payload(payload),
                status=SnapshotStatus.FRESH,
                records=tuple(records if isinstance(records, list) else []),
            )
            self.last_valid[dataset] = snapshot
            return snapshot
        except (httpx.HTTPError, ValueError):
            prior = self.last_valid.get(dataset)
            if prior:
                return prior.model_copy(update={"status": SnapshotStatus.FETCH_FAILED})
            return WeatherSnapshot(
                dataset=dataset, response_hash="", status=SnapshotStatus.FETCH_FAILED
            )


class TelegramClient:
    """Thin wrapper around the Telegram Bot API for outbound driver messages."""

    BASE_URL = "https://api.telegram.org"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10)

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> bool:
        """Send a message to a Telegram chat. Returns whether it was sent.

        Returns False (never raises) when no bot token is configured, so
        callers in the plan-activation path can send-best-effort without a
        missing credential ever failing plan activation itself. Retries 429
        and 5xx responses a few times with backoff, matching the pattern used
        by the other integration clients in this module. Pass
        ``parse_mode="HTML"`` to use Telegram's limited HTML subset (e.g. for
        a monospace ``<pre>`` block); callers are responsible for escaping
        ``<``, ``>``, and ``&`` in any text rendered that way.
        """
        if not self.settings.telegram_bot_token:
            return False
        url = f"{self.BASE_URL}/bot{self.settings.telegram_bot_token}/sendMessage"
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        for attempt in range(3):
            try:
                response = await self.client.post(url, json=payload)
            except httpx.RequestError:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (2**attempt))
                    continue
                return False
            if response.status_code == 200:
                return True
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (2**attempt))
                    continue
            return False
        return False
