"""Formatting and sending of driver-facing route dispatch messages.

Turns an activated plan's routes into a Telegram message per driver and sends
it through whatever :class:`TelegramClient` the caller provides. Kept separate
from ``api.py`` so the message text and the "who do we actually notify" logic
can be unit tested without spinning up the FastAPI app.
"""

from __future__ import annotations

from html import escape

from .domain import Coordinate, Order, PlanVersion, Vehicle, VehicleRoute

# Telegram's HTML parse mode has no <table> tag (only a small fixed subset:
# b/i/u/s/code/pre/a/spoiler — see https://core.telegram.org/bots/api#html-style).
# A real fixed-width, multi-column table would also overflow on a phone screen
# once it has to fit an address and a name. So each stop is rendered as a
# labelled block inside one <pre> region: monospace keeps the labels aligned,
# and long fields (address, customer name) can still wrap onto their own line
# instead of being cut off or breaking column alignment.
_ROW_LABELS = ("Order No.", "Customer", "Qty", "ETA", "Address", "Contact")
_LABEL_WIDTH = max(len(label) for label in _ROW_LABELS)

# Google Maps Directions deep-link caps at 8 intermediate waypoints (i.e. 10
# total points including origin and destination). For longer routes we emit
# multiple segment links, each continuing from where the previous left off.
_GMAPS_MAX_WAYPOINTS = 8

# Telegram rejects any single message over 4096 characters. We assemble the
# whole schedule into one message and only spill into additional messages when
# a route is large enough to breach this limit — so a normal route arrives as a
# single notification instead of one message per stop.
_TELEGRAM_MAX_CHARS = 4096


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


def _coord_str(coord: Coordinate) -> str:
    return f"{coord.lat},{coord.lon}"


def _gmaps_segment_url(
    origin: Coordinate,
    destination: Coordinate,
    waypoints: list[Coordinate],
) -> str:
    """Build one Google Maps Directions deep-link for a segment of the route.

    ``origin`` → ``waypoints[0..n]`` → ``destination``, all in driving mode.
    Waypoints are pipe-separated lat,lon pairs. urlencode is NOT used for the
    waypoints parameter because it would percent-encode the ``|`` separators
    that Google Maps requires as literal characters.
    """
    base = (
        "https://www.google.com/maps/dir/"
        f"?api=1"
        f"&origin={_coord_str(origin)}"
        f"&destination={_coord_str(destination)}"
        f"&travelmode=driving"
    )
    if waypoints:
        wps = "|".join(_coord_str(w) for w in waypoints)
        base += f"&waypoints={wps}"
    return base


def route_nav_links(
    depot: Coordinate,
    stops: tuple,  # tuple[RouteStop, ...]
) -> list[str]:
    """Build one or more Google Maps deep-links covering the full stop sequence.

    The route runs: depot → stop1 → stop2 → … → last stop.
    Google Maps caps at 8 intermediate waypoints per URL (10 points total), so
    routes longer than 10 stops are split into segments. Each segment starts
    from the last point of the previous one so the chain is seamless.

    Returns a list of HTML anchor strings ready to embed in a Telegram HTML
    message. Single-stop routes produce exactly one link with no waypoints.
    """
    if not stops:
        return []

    # Build a flat ordered list of all points: depot first, then each stop.
    points: list[Coordinate] = [depot] + [s.location for s in stops]

    links: list[str] = []
    # Slide a window of (_GMAPS_MAX_WAYPOINTS + 2) points at a time.
    # Window: [origin, wp1, …, wpN, destination] where N ≤ _GMAPS_MAX_WAYPOINTS.
    chunk_size = _GMAPS_MAX_WAYPOINTS + 2  # origin + up-to-8 waypoints + destination
    segment_index = 1
    i = 0
    while i < len(points) - 1:
        chunk = points[i : i + chunk_size]
        if len(chunk) < 2:
            break
        origin = chunk[0]
        destination = chunk[-1]
        waypoints = chunk[1:-1]
        url = _gmaps_segment_url(origin, destination, waypoints)
        label = (
            "Open full route in Google Maps →"
            if len(links) == 0 and i + chunk_size >= len(points)
            else f"Route segment {segment_index} in Google Maps →"
        )
        links.append(f'<a href="{url}">{escape(label)}</a>')
        # Next segment starts from the last point of this chunk so the chain
        # is seamless (the destination of this chunk is the origin of the next).
        i += chunk_size - 1
        segment_index += 1

    return links


def _stop_block(
    stop,  # RouteStop
    orders_by_id: dict[str, Order],
) -> str:
    """Render a single stop as one escaped ``<pre>`` block."""
    order = orders_by_id.get(stop.stop_id)
    eta = _format_minute(stop.eta_minute)
    fallback = f"{stop.location.lat:.4f}, {stop.location.lon:.4f}"
    if order is None:
        rows_text = f"S{stop.sequence}\n" + _stop_row("Address", fallback)
    else:
        address = order.address
        if order.postal_code:
            address += f" {order.postal_code}"
        if order.special_handling and order.special_handling != "None":
            address += f" ({order.special_handling})"
        rows_text = (
            f"S{stop.sequence}\n"
            + "\n".join([
                _stop_row("Order No.", order.order_id),
                _stop_row("Customer", order.customer_name or "-"),
                _stop_row("Qty", str(order.quantity)),
                _stop_row("ETA", eta),
                _stop_row("Address", address),
                _stop_row("Contact", order.contact_phone or "-"),
            ])
        )
    return f"<pre>{escape(rows_text)}</pre>"


def format_route_message(
    route: VehicleRoute,
    orders: tuple[Order, ...],
    plan: PlanVersion,
    fleet: tuple[Vehicle, ...] = (),
    depot: Coordinate | None = None,
) -> list[str]:
    """Render a route as Telegram message(s) with navigation links.

    Returns a **list of strings**. Normally this is a single element — the
    whole schedule (header, navigation link(s), and every stop) combined into
    one message so the driver receives one notification instead of one message
    per stop.

    The list only grows beyond one element when the assembled schedule would
    exceed Telegram's 4096-character limit; in that case the schedule is packed
    into as few messages as possible, splitting on stop boundaries so no stop
    block is ever torn across two messages.

    Message layout (within each chunk):
    * The first message always leads with the header + full-route Google Maps
      link(s), then as many stop blocks as fit.
    * Continuation messages (only for oversized routes) carry the remaining
      stop blocks.

    The Google Maps link loads all stops in the optimised sequence
    (depot → S1 → S2 → … → last stop), enforcing the routing algorithm's order.
    Routes longer than 10 stops are split into chained segment links.

    Use with ``TelegramClient.send_message(..., parse_mode="HTML")``.
    """
    orders_by_id = {order.order_id: order for order in orders}
    vehicle = next((v for v in fleet if v.vehicle_id == route.vehicle_id), None)

    header = (
        f"<b>Route assigned — Plan v{plan.version}</b>\n"
        f"Vehicle {escape(_vehicle_label(route, fleet))} · {len(route.stops)} stop(s) · "
        f"{route.distance_km:.1f} km · {_format_duration(route.duration_minutes)}"
    )

    # Navigation links — one multi-waypoint URL per segment.
    origin = depot or (vehicle.start if vehicle else None)
    if origin is None and route.stops:
        origin = route.stops[0].location
    nav_links = route_nav_links(origin, route.stops) if origin else []

    # Ordered list of every section of the schedule: header + nav links first,
    # then one block per stop. These are joined with blank lines into a single
    # message and only split apart if the whole thing overflows Telegram.
    sections: list[str] = ["\n".join([header, *nav_links])]
    sections.extend(_stop_block(stop, orders_by_id) for stop in route.stops)

    return _pack_sections(sections)


def _pack_sections(sections: list[str]) -> list[str]:
    """Combine ordered schedule sections into the fewest Telegram messages.

    Sections are joined with a blank line. Everything goes into a single
    message unless that would exceed :data:`_TELEGRAM_MAX_CHARS`, in which case
    sections are greedily packed into consecutive messages on section
    boundaries so a stop block is never split mid-way.
    """
    separator = "\n\n"
    messages: list[str] = []
    current = ""
    for section in sections:
        if not current:
            current = section
            continue
        candidate = current + separator + section
        if len(candidate) <= _TELEGRAM_MAX_CHARS:
            current = candidate
        else:
            messages.append(current)
            current = section
    if current:
        messages.append(current)
    return messages or [""]


async def send_route_messages(
    telegram_client,
    chat_id: int,
    route: VehicleRoute,
    orders: tuple[Order, ...],
    plan: PlanVersion,
    fleet: tuple[Vehicle, ...] = (),
    depot: Coordinate | None = None,
) -> bool:
    """Send the full route schedule to the driver.

    Normally this is a single Telegram message containing the header, the
    navigation link(s), and every stop, so the driver gets one notification
    instead of being spammed with one message per stop. Only routes large
    enough to exceed Telegram's 4096-character limit are split across multiple
    messages. Returns True only if every message was sent successfully. Stops
    sending on the first failure so the driver knows something went wrong
    rather than receiving a partial schedule silently.
    """
    messages = format_route_message(route, orders, plan, fleet, depot=depot)
    for msg in messages:
        sent = await telegram_client.send_message(chat_id, msg, parse_mode="HTML")
        if not sent:
            return False
    return True


def format_driver_reply(text: str) -> str:
    """Sanitise an agent reply for delivery to a driver over Telegram.

    The driver-agent prompt produces plain text (no markdown, no HTML), but
    a model can still slip in stray ``<``, ``>``, or ``&`` characters that
    would break Telegram's HTML parse mode if we ever switch to it. This
    function strips those characters so the message is safe to send with
    ``parse_mode=None`` (plain text), which is always safe regardless of
    content. Also collapses excessive whitespace so the message renders
    cleanly on a small phone screen.
    """
    # Plain-text send needs no escaping, but remove HTML-looking fragments
    # that could confuse the reader (e.g. accidental "<None>" from a model).
    sanitised = text.replace("<", "").replace(">", "").replace("&", "and")
    # Collapse runs of blank lines to a single newline.
    lines = [line.rstrip() for line in sanitised.splitlines()]
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)
    return "\n".join(collapsed).strip()
