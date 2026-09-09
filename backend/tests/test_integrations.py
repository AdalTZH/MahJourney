import httpx
import pytest

from mahjourney.config import Settings
from mahjourney.domain import SnapshotStatus
from mahjourney.integrations import LtaClient, OneMapClient


@pytest.mark.asyncio
async def test_onemap_auth_error_stops_subsequent_calls() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "expired"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OneMapClient(Settings(onemap_access_token="bad-token"), client)
    result = await adapter.health_check()
    assert result.status == SnapshotStatus.AUTHENTICATION_FAILED
    with pytest.raises(RuntimeError):
        await adapter.search("619495")
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_onemap_retries_transient_request_errors() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        return httpx.Response(
            200,
            json={
                "route_geometry": "a|`GgsxwRKuD",
                "route_summary": {"total_distance": 1000},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OneMapClient(Settings(onemap_access_token="test-token"), client)
    result = await adapter.route((1.3214, 103.6783), (1.329, 103.706))
    assert result["route_summary"]["total_distance"] == 1000
    assert calls == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_lta_follows_500_record_pagination() -> None:
    skips: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("$skip", "0"))
        skips.append(skip)
        count = 500 if skip == 0 else 3
        return httpx.Response(200, json={"value": [{"id": skip + i} for i in range(count)]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LtaClient(Settings(lta_datamall_account_key="test-key"), client)
    result = await adapter.collect("incidents")
    assert result.record_count == 503
    assert skips == [0, 500]
    await client.aclose()


@pytest.mark.asyncio
async def test_lta_speed_bands_uses_current_v4_endpoint() -> None:
    requested_path = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_path
        requested_path = request.url.path
        return httpx.Response(200, json={"value": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LtaClient(Settings(lta_datamall_account_key="test-key"), client)
    result = await adapter.collect("speed_bands")
    assert result.status == SnapshotStatus.FRESH
    assert requested_path.endswith("/v4/TrafficSpeedBands")
    await client.aclose()
