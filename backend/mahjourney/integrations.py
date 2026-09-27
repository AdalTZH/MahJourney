from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Settings
from .domain import Coordinate, IntegrationHealth, SnapshotStatus, TrafficSnapshot, WeatherSnapshot

log = logging.getLogger(__name__)

# Refresh the token this many seconds before it actually expires so callers
# never see a mid-request expiry.
_TOKEN_REFRESH_BUFFER_SECONDS = 30 * 60  # 30 minutes


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
        # Active token and its expiry. Seeded from the static env var when
        # present so the very first request works without a round-trip to the
        # auth endpoint.
        self._token: str = settings.onemap_access_token
        self._token_expiry: datetime | None = token_expiry(self._token) if self._token else None
        self._refresh_lock = asyncio.Lock()

    def _token_needs_refresh(self) -> bool:
        """True when there is no token or it expires within the buffer window."""
        if not self._token:
            return True
        if self._token_expiry is None:
            return False  # can't decode expiry; assume the static token is valid
        return datetime.now(UTC) >= self._token_expiry - timedelta(
            seconds=_TOKEN_REFRESH_BUFFER_SECONDS
        )

    async def _ensure_token(self) -> None:
        """Fetch or refresh the access token when it is missing or near-expiry.

        Uses ONEMAP_API_EMAIL + ONEMAP_API_PASSWORD to call the auth endpoint.
        Falls back gracefully if credentials are not configured (static token
        from the env var is used as-is until it expires).

        Only one coroutine fetches at a time; others wait behind the lock and
        reuse the token that was fetched while they were waiting.
        """
        if not self._token_needs_refresh():
            return

        if not self.settings.onemap_api_email or not self.settings.onemap_api_password:
            # No credentials configured — keep using whatever static token we have
            return

        async with self._refresh_lock:
            # Re-check after acquiring the lock; another coroutine may have
            # already refreshed the token while we were waiting.
            if not self._token_needs_refresh():
                return

            auth_url = f"{self.settings.onemap_base_url.rstrip('/')}/api/auth/post/getToken"
            try:
                response = await self.client.post(
                    auth_url,
                    json={
                        "email": self.settings.onemap_api_email,
                        "password": self.settings.onemap_api_password,
                    },
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
                new_token: str = data["access_token"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("OneMap token refresh failed: %s — keeping existing token", exc)
                return

            self._token = new_token
            self._token_expiry = token_expiry(new_token)
            # Clear the circuit-breaker so requests flow again after a
            # successful refresh (e.g. the old token had expired).
            self.authentication_failed = False
            log.info(
                "OneMap token refreshed; expires %s",
                self._token_expiry.isoformat() if self._token_expiry else "unknown",
            )

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.authentication_failed:
            raise RuntimeError("OneMap disabled after authentication failure")
        await self._ensure_token()
        for attempt in range(3):
            try:
                response = await self.client.get(
                    f"{self.settings.onemap_base_url.rstrip('/')}{path}",
                    params=params,
                    headers={"Authorization": self._token},
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
                # Before giving up, try to refresh the token once (the static
                # env-var token may have just expired).
                if attempt == 0 and (
                    self.settings.onemap_api_email and self.settings.onemap_api_password
                ):
                    # Force-expire the cached token so _ensure_token fetches a new one.
                    self._token_expiry = datetime.now(UTC) - timedelta(seconds=1)
                    await self._ensure_token()
                    if self._token:
                        continue  # retry the request with the fresh token
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
        has_credentials = bool(
            self.settings.onemap_api_email and self.settings.onemap_api_password
        )
        if not self._token and not has_credentials:
            return IntegrationHealth(
                integration="OneMap",
                status=SnapshotStatus.NOT_CONFIGURED,
                message="ONEMAP_ACCESS_TOKEN is blank and ONEMAP_API_EMAIL/PASSWORD are not set",
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
                expires_at=self._token_expiry,
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return IntegrationHealth(
                integration="OneMap",
                status=SnapshotStatus.FETCH_FAILED,
                latency_ms=int((time.perf_counter() - started) * 1000),
                message=f"read-only health check failed: {type(exc).__name__}",
                expires_at=self._token_expiry,
            )
        return IntegrationHealth(
            integration="OneMap",
            status=SnapshotStatus.FRESH,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message="; ".join(
                value
                for value in (
                    "Search and Routing read-only checks passed",
                    expiry_message(self._token_expiry),
                )
                if value
            ),
            expires_at=self._token_expiry,
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
            log.warning("telegram send skipped: no bot token configured (chat_id=%s)", chat_id)
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
            except httpx.RequestError as exc:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (2**attempt))
                    continue
                log.warning(
                    "telegram sendMessage failed after %d attempts (chat_id=%s): %s",
                    attempt + 1,
                    chat_id,
                    exc,
                )
                return False
            if response.status_code == 200:
                return True
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (2**attempt))
                    continue
            # Bad request / blocked bot / unknown chat: log the body, since this
            # is the only signal a dispatcher-facing "didn't go through" carries.
            log.warning(
                "telegram sendMessage rejected (chat_id=%s, status=%s): %s",
                chat_id,
                response.status_code,
                response.text[:300],
            )
            return False
        return False


async def summarize_traffic_conditions(lta: LtaClient) -> dict[str, Any] | None:
    """Best-effort summary of current LTA traffic for the disruption agent tool.

    Fetches the incidents and speed-band datasets and reduces them to a small,
    JSON-serialisable dict (incident count, a few sample incident types, and a
    freshness flag). Returns ``None`` when LTA is not configured or every fetch
    failed, so the calling tool degrades to "unavailable" rather than reporting
    fabricated data. Never raises — the clients already fail closed.
    """
    incidents = await lta.collect("incidents")
    speed_bands = await lta.collect("speed_bands")
    if (
        incidents.status is SnapshotStatus.NOT_CONFIGURED
        and speed_bands.status is SnapshotStatus.NOT_CONFIGURED
    ):
        return None
    if (
        incidents.status is SnapshotStatus.FETCH_FAILED
        and speed_bands.status is SnapshotStatus.FETCH_FAILED
        and not incidents.records
        and not speed_bands.records
    ):
        return None
    sample_types = sorted(
        {
            str(record.get("Type"))
            for record in incidents.records[:50]
            if isinstance(record, dict) and record.get("Type")
        }
    )[:5]
    return {
        "source": "LTA DataMall",
        "incident_count": incidents.record_count,
        "incident_types_sample": sample_types,
        "speed_band_segments": speed_bands.record_count,
        "fresh": incidents.status is SnapshotStatus.FRESH,
    }


async def summarize_weather_conditions(nea: NeaClient) -> dict[str, Any] | None:
    """Best-effort summary of current NEA weather for the disruption agent tool.

    Fetches rainfall readings and the two-hour forecast and reduces them to a
    small dict (whether any station reports rain, how many do, and a sample of
    forecast areas mentioning rain). Returns ``None`` when NEA is unavailable, so
    the calling tool degrades to "unavailable".
    """
    rainfall = await nea.collect("rainfall")
    forecast = await nea.collect("two_hour_forecast")
    if (
        rainfall.status is SnapshotStatus.FETCH_FAILED
        and forecast.status is SnapshotStatus.FETCH_FAILED
        and not rainfall.records
        and not forecast.records
    ):
        return None

    def _reading_value(record: Any) -> float:
        if not isinstance(record, dict):
            return 0.0
        value = record.get("value", record.get("rainfall", 0))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    raining_stations = sum(1 for r in rainfall.records if _reading_value(r) > 0)
    rain_terms = ("rain", "shower", "thunder")
    rain_areas: list[str] = []
    for record in forecast.records:
        if not isinstance(record, dict):
            continue
        text = str(record.get("forecast", "")).casefold()
        area = record.get("area") or record.get("name")
        if area and any(term in text for term in rain_terms):
            rain_areas.append(str(area))
    return {
        "source": "NEA",
        "rain_detected": raining_stations > 0 or bool(rain_areas),
        "raining_station_count": raining_stations,
        "rain_forecast_areas_sample": sorted(set(rain_areas))[:5],
        "fresh": rainfall.status is SnapshotStatus.FRESH,
    }


class GraphHopperClient:
    """Thin async wrapper around GraphHopper's Routing API.

    Supports both the hosted GraphHopper API (requires an API key) and a
    self-hosted instance (no key needed — just point GRAPHHOPPER_BASE_URL at
    the local container).  Used exclusively for reroute geometry enrichment
    when a closure is active: the ``route_avoiding`` method passes the closure
    corridor as a ``custom_model`` area with ``priority: multiply_by 0``, so
    GraphHopper hard-blocks every road segment inside the polygon and returns a
    real road path that physically avoids the closed section.

    Falls back gracefully (raises ``RuntimeError``) on any HTTP/JSON error so
    the caller can fall back to OneMap.
    """

    HOSTED_BASE_URL = "https://graphhopper.com/api/1"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=15)

    @property
    def _base_url(self) -> str:
        """Effective base URL: local container if configured, else hosted."""
        url = (self.settings.graphhopper_base_url or "").rstrip("/")
        if url and url != self.HOSTED_BASE_URL:
            return url
        return self.HOSTED_BASE_URL

    @property
    def configured(self) -> bool:
        """True when a self-hosted URL is set OR a hosted API key is present."""
        has_local = bool(
            self.settings.graphhopper_base_url
            and self.settings.graphhopper_base_url.rstrip("/") != self.HOSTED_BASE_URL
        )
        has_hosted_key = bool(self.settings.graphhopper_api_key)
        return has_local or has_hosted_key

    async def route_avoiding(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        avoid_polygon: tuple[Coordinate, ...],
    ) -> tuple[Coordinate, ...]:
        """Route start→end avoiding ``avoid_polygon`` using GraphHopper's
        custom_model area block.

        ``start`` / ``end`` are (lat, lon) tuples.
        ``avoid_polygon`` is a sequence of Coordinate objects forming the
        closure corridor ring.

        Returns a tuple of Coordinate points (the road geometry), or raises
        ``RuntimeError`` on any failure so the caller can fall back to OneMap.
        """
        from .domain import Coordinate  # local import to avoid circular deps

        if not self.configured:
            raise RuntimeError("GraphHopper is not configured")

        # GraphHopper expects [lon, lat] coordinate order in GeoJSON.
        ring = [[pt.lon, pt.lat] for pt in avoid_polygon]
        # Close the ring if not already closed.
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])

        payload = {
            "points": [
                [start[1], start[0]],   # [lon, lat]
                [end[1], end[0]],
            ],
            "profile": self.settings.graphhopper_profile or "car",
            "points_encoded": False,
            "ch.disable": True,
            "custom_model": {
                "priority": [{"if": "in_closure_zone", "multiply_by": "0"}],
                "areas": {
                    "closure_zone": {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [ring],
                        },
                    }
                },
            },
        }

        url = f"{self._base_url}/route"
        # API key is optional for self-hosted instances.
        params: dict[str, str] = {}
        if self.settings.graphhopper_api_key:
            params["key"] = self.settings.graphhopper_api_key

        try:
            response = await self.client.post(url, params=params, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GraphHopper HTTP error: {exc}") from exc

        try:
            points = data["paths"][0]["points"]["coordinates"]
            return tuple(
                Coordinate(lat=float(pt[1]), lon=float(pt[0])) for pt in points
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"GraphHopper response parse error: {exc}") from exc
