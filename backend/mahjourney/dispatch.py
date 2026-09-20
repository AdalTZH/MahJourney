"""Formatting and sending of driver-facing route dispatch messages.

Turns an activated plan's routes into a Telegram message per driver and sends
it through whatever :class:`TelegramClient` the caller provides. Kept separate
from ``api.py`` so the message text and the "who do we actually notify" logic
can be unit tested without spinning up the FastAPI app.
"""

from __future__ import annotations

from html import escape

from .domain import Order, PlanVersion, Vehicle, VehicleRoute

# Telegram's HTML parse mode has no <table> tag (only a small fixed subset:
# b/i/u/s/code/pre/a/spoiler — see https://core.telegram.org/bots/api#html-style).
# A real fixed-width, multi-column table would also overflow on a phone screen
# once it has to fit an address and a name. So each stop is rendered as a
# labelled block inside one <pre> region: monospace keeps the labels aligned,
# and long fields (address, customer name) can still wrap onto their own line
# instead of being cut off or breaking column alignment.
_ROW_LABELS = ("Order No.", "Customer", "Qty", "ETA", "Address", "Contact")
_LABEL_WIDTH = max(len(label) for label in _ROW_LABELS)


def _format_minute(minute: int) -> str:
    hour, remainder = divmod(minute, 60)
    return f"{hour % 24:02d}:{remainder:02d}"


def _format_duration(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours} hr {remainder} min"
    if hours:
        return f"{hours} hr"
    return f"{remainder} min"


def _vehicle_label(route: VehicleRoute, fleet: tuple[Vehicle, ...]) -> str:
    """The vehicle's license plate, falling back to its id if unknown."""
    vehicle = next((v for v in fleet if v.vehicle_id == route.vehicle_id), None)
    if vehicle is not None and vehicle.license_plate:
        return vehicle.license_plate
    return route.vehicle_id


def _stop_row(label: str, value: str) -> str:
    return f"{label:<{_LABEL_WIDTH}} : {value}"


def _stop_block(sequence: int, order: Order | None, eta: str, fallback: str) -> str:
    if order is None:
        return f"S{sequence}\n" + _stop_row("Address", fallback)
    address = order.address
    if order.postal_code:
        address += f" {order.postal_code}"
    if order.special_handling and order.special_handling != "None":
        address += f" ({order.special_handling})"
    rows = [
        _stop_row("Order No.", order.order_id),
        _stop_row("Customer", order.customer_name or "-"),
        _stop_row("Qty", str(order.quantity)),
        _stop_row("ETA", eta),
        _stop_row("Address", address),
        _stop_row("Contact", order.contact_phone or "-"),
    ]
    return f"S{sequence}\n" + "\n".join(rows)


def format_route_message(
    route: VehicleRoute,
    orders: tuple[Order, ...],
    plan: PlanVersion,
    fleet: tuple[Vehicle, ...] = (),
) -> str:
    """Render a route as a driver-facing dispatch message.

    Each stop is described using the order it was built from (``stop_id`` is
    always an ``order_id``, see ``planning.py``); stops with no matching order
    fall back to their coordinates so a message can still be sent. The vehicle
    is shown by license plate when ``fleet`` has a matching record, otherwise
    by its internal vehicle id.

    Stop details are rendered as a monospace ``<pre>`` block (Telegram HTML
    parse mode) so the "S/N · field : value" rows line up like a table without
    truncating long addresses or customer names. Use with
    ``TelegramClient.send_message(..., parse_mode="HTML")``.
    """
    orders_by_id = {order.order_id: order for order in orders}
    header = (
        f"New route assigned — Plan v{plan.version}\n"
        f"Vehicle {_vehicle_label(route, fleet)} · {len(route.stops)} stop(s) · "
        f"{route.distance_km:.1f} km · {_format_duration(route.duration_minutes)}"
    )
    blocks = []
    for stop in route.stops:
        order = orders_by_id.get(stop.stop_id)
        eta = _format_minute(stop.eta_minute)
        fallback = f"{stop.location.lat:.4f}, {stop.location.lon:.4f}"
        blocks.append(_stop_block(stop.sequence, order, eta, fallback))
    table = escape("\n\n".join(blocks))
    return f"{escape(header)}\n\n<pre>{table}</pre>"
