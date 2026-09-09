import httpx

from mahjourney.domain import Coordinate
from mahjourney.fixtures import synthetic_fleet, synthetic_orders
from mahjourney.planning import build_plan
from mahjourney.route_geometry import decode_polyline, enrich_plan_geometry
from mahjourney.simulation import point_along_geometry


def test_onemap_polyline_decodes_and_interpolates() -> None:
    geometry = decode_polyline(
        "a|`GgsxwRKuDcGK}BEm@Ac@eAKo@CcPZ{EAgg@Dcd@NmFp@{PlAyXGcFNgCmI[eX^?O"
    )
    assert len(geometry) >= 2
    assert geometry[0].lat == 1.32049
    assert geometry[0].lon == 103.67812
    midpoint = point_along_geometry(
        (Coordinate(lat=1.3, lon=103.7), Coordinate(lat=1.4, lon=103.8)), 0.5
    )
    assert round(midpoint.lat, 3) == 1.35


async def test_plan_geometry_enrichment_pairs_every_selected_leg() -> None:
    class FakeOneMap:
        calls = 0

        async def route(self, start, end):
            self.calls += 1
            return {
                "route_geometry": (
                    "a|`GgsxwRKuDcGK}BEm@Ac@eAKo@CcPZ{EAgg@Dcd@NmFp@{PlAyXGcFNgCmI[eX^?O"
                ),
                "route_summary": {"total_distance": 1000},
            }

    plan = build_plan(synthetic_fleet(), synthetic_orders())
    client = FakeOneMap()
    enriched = await enrich_plan_geometry(plan, synthetic_fleet()[0].start, client)
    assert client.calls == 50
    assert all(route.geometry for route in enriched.routes)


async def test_failed_leg_is_not_replaced_with_a_straight_line() -> None:
    plan = build_plan(synthetic_fleet(), synthetic_orders())
    failed_start = synthetic_fleet()[0].start
    failed_end = plan.routes[0].stops[0].location

    class FakeOneMap:
        async def route(self, start, end):
            if start == (failed_start.lat, failed_start.lon) and end == (
                failed_end.lat,
                failed_end.lon,
            ):
                raise httpx.ReadTimeout("temporary timeout")
            return {
                "route_geometry": (
                    "a|`GgsxwRKuDcGK}BEm@Ac@eAKo@CcPZ{EAgg@Dcd@NmFp@{PlAyXGcFNgCmI[eX^?O"
                ),
                "route_summary": {"total_distance": 1000},
            }

    enriched = await enrich_plan_geometry(plan, failed_start, FakeOneMap())
    assert enriched.routes[0].geometry == ()
    assert all(route.geometry for route in enriched.routes[1:])
