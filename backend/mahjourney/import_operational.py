"""Load the Singapore logistics workbook into PostgreSQL.

Usage (from the backend directory, with persistence configured)::

    python -m mahjourney.import_operational \
        --workbook ../database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx

The script is idempotent: it applies the operational-data migration if needed
and upserts every row keyed on its primary id, so re-running refreshes the data
without duplicating it.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .config import get_settings
from .operational_data import (
    as_id,
    as_str,
    coordinates_valid,
    parse_bool,
    parse_date,
    parse_int,
    parse_number,
    parse_time_to_minute,
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "004_operational_data.sql"


def _sheet_rows(worksheet: Any) -> list[dict[str, Any]]:
    rows = worksheet.iter_rows(values_only=True)
    header = [str(cell).strip() if cell is not None else "" for cell in next(rows)]
    records = []
    for raw in rows:
        if all(cell is None for cell in raw):
            continue
        records.append(dict(zip(header, raw, strict=False)))
    return records


def load_workbook_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    needed = ("Depots", "Drivers", "Vehicles", "Orders")
    missing = [name for name in needed if name not in workbook.sheetnames]
    if missing:
        raise ValueError(f"workbook is missing required sheets: {missing}")
    return {name: _sheet_rows(workbook[name]) for name in needed}


def _depot_params(row: dict[str, Any]) -> dict[str, Any] | None:
    lat, lon = parse_number(row.get("Latitude")), parse_number(row.get("Longitude"))
    if not coordinates_valid(lat, lon):
        return None
    return {
        "depot_id": as_id(row.get("Depot ID")),
        "name": as_str(row.get("Depot Name")),
        "address": as_str(row.get("Address")),
        "postal_code": as_str(row.get("Postal Code")),
        "latitude": lat,
        "longitude": lon,
        "delivery_area": as_str(row.get("Delivery Area")),
        "operating_start_minute": parse_time_to_minute(row.get("Operating Hours Start")) or 0,
        "operating_end_minute": parse_time_to_minute(row.get("Operating Hours End")) or 1439,
        "storage_capacity_m3": parse_number(row.get("Storage Capacity (m³)")),
        "vehicle_capacity": parse_int(row.get("Vehicle Capacity")),
        "cold_storage": parse_bool(row.get("Cold Storage Available")),
        "loading_bays": parse_int(row.get("Loading Bays")),
        "status": as_str(row.get("Depot Status")) or "Operational",
    }


def _driver_params(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "driver_id": as_id(row.get("Driver ID")),
        "name": as_str(row.get("Driver Name")),
        "depot_id": as_id(row.get("Depot ID")) or None,
        "license_type": as_str(row.get("License Type")),
        "vocational_license": as_str(row.get("Vocational License")),
        "certification_type": as_str(row.get("Certification Type")),
        "working_start_minute": parse_time_to_minute(row.get("Working Hours Start")) or 480,
        "working_end_minute": parse_time_to_minute(row.get("Working Hours End")) or 1080,
        "shift_type": as_str(row.get("Shift Type")),
        "home_depot": as_str(row.get("Home Depot")),
        "skill_set": as_str(row.get("Skill Set")),
        "availability_status": as_str(row.get("Availability Status")) or "Available",
    }


def _vehicle_params(row: dict[str, Any]) -> dict[str, Any] | None:
    lat, lon = parse_number(row.get("Current Latitude")), parse_number(row.get("Current Longitude"))
    if not coordinates_valid(lat, lon):
        return None
    return {
        "vehicle_id": as_id(row.get("Vehicle ID")),
        "license_plate": as_str(row.get("License Plate")),
        "vehicle_type": as_str(row.get("Vehicle Type")),
        "vehicle_make": as_str(row.get("Vehicle Make")),
        "vehicle_model": as_str(row.get("Vehicle Model")),
        "vehicle_year": parse_int(row.get("Vehicle Year")),
        "capacity_weight_kg": parse_number(row.get("Capacity Weight (kg)")) or 0.0,
        "capacity_volume_m3": parse_number(row.get("Capacity Volume (m³)")) or 0.0,
        "lta_vehicle_class": as_str(row.get("LTA Vehicle Class")),
        "fuel_type": as_str(row.get("Fuel Type")),
        "fuel_consumption_l_per_100km": parse_number(row.get("Fuel Consumption (L/100km)")),
        "iu_status": as_str(row.get("IU Status")),
        "current_depot": as_str(row.get("Current Depot")),
        "depot_id": as_id(row.get("Depot ID")) or None,
        "current_latitude": lat,
        "current_longitude": lon,
        "availability_start_minute": parse_time_to_minute(row.get("Availability Start")) or 0,
        "availability_end_minute": parse_time_to_minute(row.get("Availability End")) or 1439,
        "special_equipment": as_str(row.get("Special Equipment")),
        "refrigeration_capability": parse_bool(row.get("Refrigeration Capability")),
        "vehicle_availability": as_str(row.get("Vehicle Availability")) or "Available",
        "vehicle_status": as_str(row.get("Vehicle Status")) or "In Fleet",
    }


def _order_params(row: dict[str, Any]) -> dict[str, Any] | None:
    lat, lon = parse_number(row.get("Latitude")), parse_number(row.get("Longitude"))
    if not coordinates_valid(lat, lon):
        return None
    return {
        "order_id": as_id(row.get("Order ID")),
        "customer_id": as_id(row.get("Customer ID")),
        "customer_name": as_str(row.get("Customer Name")),
        "delivery_address": as_str(row.get("Delivery Address")),
        "postal_code": as_str(row.get("Postal Code")),
        "latitude": lat,
        "longitude": lon,
        "delivery_area": as_str(row.get("Delivery Area")),
        "window_start_minute": parse_time_to_minute(row.get("Delivery Time Window Start")) or 480,
        "window_end_minute": parse_time_to_minute(row.get("Delivery Time Window End")) or 1080,
        "order_date": parse_date(row.get("Order Date")),
        "requested_delivery_date": parse_date(row.get("Requested Delivery Date")),
        "shipping_method": as_str(row.get("Shipping Method")),
        "priority_level": parse_int(row.get("Priority Level")) or 3,
        "quantity": max(1, parse_int(row.get("Quantity")) or 1),
        "weight_kg": parse_number(row.get("Weight (kg)")) or 0.0,
        "volume_m3": parse_number(row.get("Volume (m³)")) or 0.0,
        "special_handling": as_str(row.get("Special Handling")) or "None",
        "delivery_type": as_str(row.get("Delivery Type")),
        "contact_phone": as_str(row.get("Contact Phone")),
        "special_instructions": as_str(row.get("Special Instructions")),
        "order_status": as_str(row.get("Order Status")) or "Pending Dispatch",
    }


_DEPOT_UPSERT = text(
    """
    INSERT INTO depots(
        depot_id, name, address, postal_code, latitude, longitude, location,
        delivery_area, operating_start_minute, operating_end_minute,
        storage_capacity_m3, vehicle_capacity, cold_storage, loading_bays, status
    ) VALUES (
        :depot_id, :name, :address, :postal_code, :latitude, :longitude,
        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326),
        :delivery_area, :operating_start_minute, :operating_end_minute,
        :storage_capacity_m3, :vehicle_capacity, :cold_storage, :loading_bays, :status
    )
    ON CONFLICT (depot_id) DO UPDATE SET
        name = EXCLUDED.name, address = EXCLUDED.address, postal_code = EXCLUDED.postal_code,
        latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, location = EXCLUDED.location,
        delivery_area = EXCLUDED.delivery_area,
        operating_start_minute = EXCLUDED.operating_start_minute,
        operating_end_minute = EXCLUDED.operating_end_minute,
        storage_capacity_m3 = EXCLUDED.storage_capacity_m3,
        vehicle_capacity = EXCLUDED.vehicle_capacity, cold_storage = EXCLUDED.cold_storage,
        loading_bays = EXCLUDED.loading_bays, status = EXCLUDED.status
    """
)

_DRIVER_UPSERT = text(
    """
    INSERT INTO drivers(
        driver_id, name, depot_id, license_type, vocational_license, certification_type,
        working_start_minute, working_end_minute, shift_type, home_depot, skill_set,
        availability_status
    ) VALUES (
        :driver_id, :name, :depot_id, :license_type, :vocational_license, :certification_type,
        :working_start_minute, :working_end_minute, :shift_type, :home_depot, :skill_set,
        :availability_status
    )
    ON CONFLICT (driver_id) DO UPDATE SET
        name = EXCLUDED.name, depot_id = EXCLUDED.depot_id, license_type = EXCLUDED.license_type,
        vocational_license = EXCLUDED.vocational_license,
        certification_type = EXCLUDED.certification_type,
        working_start_minute = EXCLUDED.working_start_minute,
        working_end_minute = EXCLUDED.working_end_minute, shift_type = EXCLUDED.shift_type,
        home_depot = EXCLUDED.home_depot, skill_set = EXCLUDED.skill_set,
        availability_status = EXCLUDED.availability_status
    """
)

_VEHICLE_UPSERT = text(
    """
    INSERT INTO vehicles(
        vehicle_id, license_plate, vehicle_type, vehicle_make, vehicle_model, vehicle_year,
        capacity_weight_kg, capacity_volume_m3, lta_vehicle_class, fuel_type,
        fuel_consumption_l_per_100km, iu_status, current_depot, depot_id,
        current_latitude, current_longitude, location,
        availability_start_minute, availability_end_minute, special_equipment,
        refrigeration_capability, vehicle_availability, vehicle_status
    ) VALUES (
        :vehicle_id, :license_plate, :vehicle_type, :vehicle_make, :vehicle_model, :vehicle_year,
        :capacity_weight_kg, :capacity_volume_m3, :lta_vehicle_class, :fuel_type,
        :fuel_consumption_l_per_100km, :iu_status, :current_depot, :depot_id,
        :current_latitude, :current_longitude,
        ST_SetSRID(ST_MakePoint(:current_longitude, :current_latitude), 4326),
        :availability_start_minute, :availability_end_minute, :special_equipment,
        :refrigeration_capability, :vehicle_availability, :vehicle_status
    )
    ON CONFLICT (vehicle_id) DO UPDATE SET
        license_plate = EXCLUDED.license_plate, vehicle_type = EXCLUDED.vehicle_type,
        vehicle_make = EXCLUDED.vehicle_make, vehicle_model = EXCLUDED.vehicle_model,
        vehicle_year = EXCLUDED.vehicle_year, capacity_weight_kg = EXCLUDED.capacity_weight_kg,
        capacity_volume_m3 = EXCLUDED.capacity_volume_m3,
        lta_vehicle_class = EXCLUDED.lta_vehicle_class, fuel_type = EXCLUDED.fuel_type,
        fuel_consumption_l_per_100km = EXCLUDED.fuel_consumption_l_per_100km,
        iu_status = EXCLUDED.iu_status, current_depot = EXCLUDED.current_depot,
        depot_id = EXCLUDED.depot_id, current_latitude = EXCLUDED.current_latitude,
        current_longitude = EXCLUDED.current_longitude, location = EXCLUDED.location,
        availability_start_minute = EXCLUDED.availability_start_minute,
        availability_end_minute = EXCLUDED.availability_end_minute,
        special_equipment = EXCLUDED.special_equipment,
        refrigeration_capability = EXCLUDED.refrigeration_capability,
        vehicle_availability = EXCLUDED.vehicle_availability,
        vehicle_status = EXCLUDED.vehicle_status
    """
)

_ORDER_UPSERT = text(
    """
    INSERT INTO orders(
        order_id, customer_id, customer_name, delivery_address, postal_code,
        latitude, longitude, location, delivery_area, window_start_minute, window_end_minute,
        order_date, requested_delivery_date, shipping_method, priority_level, quantity,
        weight_kg, volume_m3, special_handling, delivery_type, contact_phone,
        special_instructions, order_status
    ) VALUES (
        :order_id, :customer_id, :customer_name, :delivery_address, :postal_code,
        :latitude, :longitude, ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326),
        :delivery_area, :window_start_minute, :window_end_minute,
        :order_date, :requested_delivery_date, :shipping_method, :priority_level, :quantity,
        :weight_kg, :volume_m3, :special_handling, :delivery_type, :contact_phone,
        :special_instructions, :order_status
    )
    ON CONFLICT (order_id) DO UPDATE SET
        customer_id = EXCLUDED.customer_id, customer_name = EXCLUDED.customer_name,
        delivery_address = EXCLUDED.delivery_address, postal_code = EXCLUDED.postal_code,
        latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, location = EXCLUDED.location,
        delivery_area = EXCLUDED.delivery_area, window_start_minute = EXCLUDED.window_start_minute,
        window_end_minute = EXCLUDED.window_end_minute, order_date = EXCLUDED.order_date,
        requested_delivery_date = EXCLUDED.requested_delivery_date,
        shipping_method = EXCLUDED.shipping_method, priority_level = EXCLUDED.priority_level,
        quantity = EXCLUDED.quantity, weight_kg = EXCLUDED.weight_kg,
        volume_m3 = EXCLUDED.volume_m3, special_handling = EXCLUDED.special_handling,
        delivery_type = EXCLUDED.delivery_type, contact_phone = EXCLUDED.contact_phone,
        special_instructions = EXCLUDED.special_instructions, order_status = EXCLUDED.order_status
    """
)


async def import_workbook(
    workbook_path: Path, database_url: str, replace: bool = False
) -> dict[str, int | str]:
    sheets = load_workbook_rows(workbook_path)
    depots = [params for row in sheets["Depots"] if (params := _depot_params(row))]
    drivers = [_driver_params(row) for row in sheets["Drivers"]]
    vehicles = [params for row in sheets["Vehicles"] if (params := _vehicle_params(row))]
    orders = [params for row in sheets["Orders"] if (params := _order_params(row))]

    known_depots = {depot["depot_id"] for depot in depots}
    for driver in drivers:
        if driver["depot_id"] not in known_depots:
            driver["depot_id"] = None
    for vehicle in vehicles:
        if vehicle["depot_id"] not in known_depots:
            vehicle["depot_id"] = None

    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    try:
        async with engine.begin() as connection:
            for statement in _split_sql(MIGRATION.read_text(encoding="utf-8")):
                await connection.execute(text(statement))
        async with engine.begin() as connection:
            if replace:
                # Full refresh: clear existing rows so deletions in the workbook
                # are reflected. Children before parents to respect foreign keys.
                for table in ("orders", "vehicles", "drivers", "depots"):
                    await connection.execute(text(f"DELETE FROM {table}"))  # noqa: S608
                # Every persisted plan was built from data that no longer exists,
                # so clear stale plan rows (and their approvals, which reference
                # them) to keep the table tidy. The app rebuilds a fresh plan on
                # its next startup from the reloaded data.
                await connection.execute(text("DELETE FROM approval_requests"))
                await connection.execute(text("DELETE FROM plan_versions"))
            if depots:
                await connection.execute(_DEPOT_UPSERT, depots)
            if drivers:
                await connection.execute(_DRIVER_UPSERT, drivers)
            if vehicles:
                await connection.execute(_VEHICLE_UPSERT, vehicles)
            if orders:
                await connection.execute(_ORDER_UPSERT, orders)
    finally:
        await engine.dispose()
    return {
        "depots": len(depots),
        "drivers": len(drivers),
        "vehicles": len(vehicles),
        "orders": len(orders),
        "mode": "replace" if replace else "upsert",
    }


def _split_sql(script: str) -> list[str]:
    return [chunk.strip() for chunk in script.split(";") if chunk.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import operational fleet data from Excel.")
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path("../database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx"),
        help="Path to the .xlsx workbook.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override the async SQLAlchemy database URL (defaults to settings).",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Clear the depots/drivers/vehicles/orders tables before importing so "
            "rows deleted from the workbook are removed from the database. Without "
            "this flag the importer only inserts/updates (upsert)."
        ),
    )
    args = parser.parse_args()
    database_url = args.database_url or get_settings().database_url
    counts = asyncio.run(import_workbook(args.workbook.resolve(), database_url, args.replace))
    print("Imported:", counts)


if __name__ == "__main__":
    main()
