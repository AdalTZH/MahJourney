from mahjourney.dispatch import format_route_message
from mahjourney.domain import Coordinate, Order, PlanVersion, RouteStop, Vehicle, VehicleRoute


def _stop(stop_id: str, sequence: int, eta_minute: int) -> RouteStop:
    return RouteStop(
        stop_id=stop_id,
        sequence=sequence,
        location=Coordinate(lat=1.32, lon=103.7),
        eta_minute=eta_minute,
        departure_minute=eta_minute + 5,
        demand=1,
    )


def _plan(routes: tuple[VehicleRoute, ...]) -> PlanVersion:
    return PlanVersion(
        plan_id="plan-1",
        version=2,
        status="ACTIVE",
        source_data_version="fixture-v1",
        routes=routes,
        objective_cost=10.0,
    )


def test_format_route_message_renders_stop_table_fields() -> None:
    route = VehicleRoute(
        vehicle_id="TRK-01",
        driver_id="DRV-01",
        stops=(_stop("ORD-1", 1, 480),),
        distance_km=12.3,
        duration_minutes=95,
    )
    orders = (
        Order(
            order_id="ORD-1",
            address="123 Example Street",
            postal_code="123456",
            location=Coordinate(lat=1.32, lon=103.7),
            special_handling="Fragile",
            customer_name="Darren Tay",
            quantity=3,
            contact_phone="+65 9855 1414",
        ),
    )
    fleet = (
        Vehicle(
            vehicle_id="TRK-01",
            driver_id="DRV-01",
            start=Coordinate(lat=1.32, lon=103.7),
            license_plate="SGX1234A",
        ),
    )
    message = format_route_message(route, orders, _plan((route,)), fleet)
    assert "Vehicle SGX1234A" in message
    assert "1 hr 35 min" in message
    assert "<pre>" in message and "</pre>" in message
    assert "S1" in message
    assert "Order No. : ORD-1" in message
    assert "Customer  : Darren Tay" in message
    assert "Qty       : 3" in message
    assert "ETA       : 08:00" in message
    assert "Address   : 123 Example Street 123456 (Fragile)" in message
    assert "Contact   : +65 9855 1414" in message


def test_format_route_message_falls_back_to_coordinates_without_order() -> None:
    route = VehicleRoute(
        vehicle_id="TRK-02",
        driver_id="DRV-02",
        stops=(_stop("ORD-missing", 1, 500),),
        distance_km=5.0,
        duration_minutes=45,
    )
    message = format_route_message(route, (), _plan((route,)))
    assert "Vehicle TRK-02" in message
    assert "45 min" in message
    assert "S1" in message
    assert "Address   : 1.3200, 103.7000" in message


def test_format_route_message_escapes_html_special_characters() -> None:
    route = VehicleRoute(
        vehicle_id="TRK-03",
        driver_id="DRV-03",
        stops=(_stop("ORD-2", 1, 480),),
        distance_km=1.0,
        duration_minutes=10,
    )
    orders = (
        Order(
            order_id="ORD-2",
            address="Block <5> & Co",
            location=Coordinate(lat=1.32, lon=103.7),
            customer_name="A & B Pte Ltd",
        ),
    )
    message = format_route_message(route, orders, _plan((route,)))
    assert "<5>" not in message
    assert "&lt;5&gt;" in message
    assert "A &amp; B Pte Ltd" in message


def test_format_duration_hours_only() -> None:
    route = VehicleRoute(
        vehicle_id="TRK-04",
        driver_id="DRV-04",
        stops=(),
        distance_km=1.0,
        duration_minutes=120,
    )
    message = format_route_message(route, (), _plan((route,)))
    assert "2 hr" in message
    assert "2 hr 0 min" not in message
