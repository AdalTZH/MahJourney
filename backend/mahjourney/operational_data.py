"""Shared normalization helpers for operational fleet data.

The Excel workbook stores values in human-friendly forms (``"5.8 kg"``,
``"1,800.0 kg"``, ``"06:30"``, numeric IDs). These helpers convert them into
the normalized types used by the database schema and the domain models so the
import script and the repository agree on the mapping.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# Singapore coordinate envelope, matching domain.Coordinate validation bounds.
LAT_MIN, LAT_MAX = 1.15, 1.5
LON_MIN, LON_MAX = 103.55, 104.1

_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


def as_str(value: Any) -> str:
    """Normalize a cell to a trimmed string, dropping trailing ``.0`` on ints."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def as_id(value: Any) -> str:
    return as_str(value)


def parse_number(value: Any) -> float | None:
    """Extract a float from numbers or unit-bearing strings like ``"1,800 kg"``."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value))
    if not match:
        return None
    return float(match.group().replace(",", ""))


def parse_int(value: Any) -> int | None:
    number = parse_number(value)
    return int(round(number)) if number is not None else None


def parse_time_to_minute(value: Any) -> int | None:
    """Convert ``"06:30"``, ``datetime.time`` or ``"6.30"`` into minutes past midnight."""
    if value is None or value == "":
        return None
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return value.hour * 60 + value.minute
    text = str(value).strip()
    match = re.match(r"^(\d{1,2})[:.](\d{2})", text)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        return min(1439, hours * 60 + minutes)
    number = parse_number(text)
    if number is None:
        return None
    return min(1439, int(number) * 60)


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_bool(value: Any) -> bool:
    return str(value).strip().casefold() in {"yes", "true", "1", "y", "available", "active"}


def coordinates_valid(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and LAT_MIN <= lat <= LAT_MAX
        and LON_MIN <= lon <= LON_MAX
    )


def split_skills(value: Any) -> tuple[str, ...]:
    text = as_str(value)
    if not text:
        return ()
    parts = re.split(r"[;,/]", text)
    return tuple(part.strip() for part in parts if part.strip())
