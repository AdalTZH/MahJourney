from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .domain import (
    ApprovalRequest,
    AuditEvent,
    ConversationMessage,
    Coordinate,
    Depot,
    DisruptionEvent,
    Driver,
    MemoryItem,
    Order,
    PlanVersion,
    Vehicle,
)
from .operational_data import split_skills


def _pair_drivers_by_shift(drivers: tuple[Driver, ...]) -> dict[str, list[Driver]]:
    """Order each depot's drivers so vehicles get spread across shifts.

    Vehicles are paired to drivers by popping from the front of each depot's
    list. If drivers were left in id order, one shift (whichever sorts first)
    could claim every vehicle and leave the depot's evening drivers unused. To
    avoid that, drivers are grouped by their (start, end) shift and then
    interleaved round-robin across shift groups. With, say, 4 day-shift and 1
    evening driver at a depot with 4 vehicles, the evening driver is picked up
    on the second assignment instead of last, giving the depot real evening
    coverage from the drivers it already has.
    """
    by_depot: dict[str, dict[tuple[int, int], list[Driver]]] = {}
    for driver in drivers:
        shift = (driver.working_start_minute, driver.working_end_minute)
        by_depot.setdefault(driver.depot_id, {}).setdefault(shift, []).append(driver)

    ordered: dict[str, list[Driver]] = {}
    for depot_id, shift_groups in by_depot.items():
        # Sort shift groups by latest end first so evening shifts are offered
        # early; sort drivers within a shift by id for determinism.
        groups = [
            sorted(group, key=lambda d: d.driver_id)
            for _, group in sorted(shift_groups.items(), key=lambda item: -item[0][1])
        ]
        interleaved: list[Driver] = []
        index = 0
        while any(index < len(group) for group in groups):
            for group in groups:
                if index < len(group):
                    interleaved.append(group[index])
            index += 1
        ordered[depot_id] = interleaved
    return ordered


class PostgresRepository:
    """Persistence for dispatcher state that must survive API restarts."""

    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_size=2, max_overflow=0)

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def load_plans(self) -> tuple[PlanVersion, ...]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT payload FROM plan_versions ORDER BY created_at, version")
                )
            ).scalars()
            return tuple(PlanVersion.model_validate(payload) for payload in rows)

    async def save_plan(self, plan: PlanVersion) -> None:
        statement = text(
            """
            INSERT INTO plan_versions(
                plan_id, version, status, source_data_version,
                objective_cost, payload, created_at
            ) VALUES (
                CAST(:plan_id AS uuid), :version, :status, :source_data_version,
                :objective_cost, CAST(:payload AS jsonb), :created_at
            )
            ON CONFLICT (plan_id, version) DO UPDATE
            SET status = EXCLUDED.status, payload = EXCLUDED.payload
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                statement,
                {
                    "plan_id": plan.plan_id,
                    "version": plan.version,
                    "status": plan.status,
                    "source_data_version": plan.source_data_version,
                    "objective_cost": plan.objective_cost,
                    "payload": plan.model_dump_json(),
                    "created_at": plan.created_at,
                },
            )

    async def load_memory(self) -> tuple[MemoryItem, ...]:
        statement = text(
            """
            SELECT memory_id, kind, content, status, trust_label, supersedes_id, created_at
            FROM memory_items ORDER BY created_at
            """
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings()
            items = []
            for row in rows:
                values = dict(row)
                values["memory_id"] = str(values["memory_id"])
                if values["supersedes_id"] is not None:
                    values["supersedes_id"] = str(values["supersedes_id"])
                items.append(MemoryItem.model_validate(values))
            return tuple(items)

    async def save_memory(
        self, item: MemoryItem, embedding: tuple[float, ...] | None = None
    ) -> None:
        statement = text(
            """
            INSERT INTO memory_items(
                memory_id, kind, content, embedding, status, trust_label,
                supersedes_id, created_at
            ) VALUES (
                CAST(:memory_id AS uuid), :kind, :content, CAST(:embedding AS vector),
                :status, :trust_label, CAST(:supersedes_id AS uuid), :created_at
            )
            ON CONFLICT (memory_id) DO UPDATE
            SET content = EXCLUDED.content, status = EXCLUDED.status,
                trust_label = EXCLUDED.trust_label, supersedes_id = EXCLUDED.supersedes_id,
                embedding = COALESCE(EXCLUDED.embedding, memory_items.embedding)
            """
        )
        values = item.model_dump()
        values["embedding"] = (
            "[" + ",".join(str(value) for value in embedding) + "]" if embedding else None
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement, values)

    async def search_curated_memory(
        self, query: str, embedding: tuple[float, ...] | None, limit: int = 10
    ) -> tuple[MemoryItem, ...]:
        vector = "[" + ",".join(str(value) for value in embedding) + "]" if embedding else None
        statement = text(
            """
            SELECT memory_id, kind, content, status, trust_label, supersedes_id, created_at
            FROM memory_items
            WHERE status = 'CURATED'
            ORDER BY (
                0.45 * ts_rank_cd(to_tsvector('english', content),
                                  websearch_to_tsquery('english', :query))
                + 0.55 * CASE
                    WHEN embedding IS NOT NULL AND CAST(:embedding AS vector) IS NOT NULL
                    THEN 1 - (embedding <=> CAST(:embedding AS vector))
                    ELSE 0
                  END
            ) DESC, created_at DESC
            LIMIT :limit
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    statement, {"query": query, "embedding": vector, "limit": limit}
                )
            ).mappings()
            items = []
            for row in rows:
                values = dict(row)
                values["memory_id"] = str(values["memory_id"])
                if values["supersedes_id"] is not None:
                    values["supersedes_id"] = str(values["supersedes_id"])
                items.append(MemoryItem.model_validate(values))
            return tuple(items)

    async def save_conversation_message(self, message: ConversationMessage) -> None:
        statement = text(
            """
            INSERT INTO conversation_messages(
                message_id, conversation_id, role, content, trust_label, created_at, expires_at
            ) VALUES (
                CAST(:message_id AS uuid), :conversation_id, :role, :content,
                :trust_label, :created_at, :expires_at
            ) ON CONFLICT (message_id) DO NOTHING
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement, message.model_dump())

    async def search_conversations(
        self, query: str, limit: int = 20
    ) -> tuple[ConversationMessage, ...]:
        statement = text(
            """
            SELECT message_id, conversation_id, role, content, trust_label,
                   created_at, expires_at
            FROM conversation_messages
            WHERE expires_at > now()
              AND to_tsvector('english', content) @@ websearch_to_tsquery('english', :query)
            ORDER BY ts_rank_cd(to_tsvector('english', content),
                                websearch_to_tsquery('english', :query)) DESC,
                     created_at DESC
            LIMIT :limit
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(statement, {"query": query, "limit": limit})
            ).mappings()
            messages = []
            for row in rows:
                values = dict(row)
                values["message_id"] = str(values["message_id"])
                messages.append(ConversationMessage.model_validate(values))
            return tuple(messages)

    async def prune_expired_conversations(self) -> int:
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM conversation_messages WHERE expires_at <= now()")
            )
            return result.rowcount

    async def load_approvals(self) -> tuple[tuple[ApprovalRequest, ...], set[str]]:
        statement = text(
            """
            SELECT approval_id, plan_id, plan_version, action_digest, status,
                   created_at, proof_nonce
            FROM approval_requests ORDER BY created_at
            """
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        requests = []
        for row in rows:
            values = {key: value for key, value in row.items() if key != "proof_nonce"}
            values["approval_id"] = str(values["approval_id"])
            values["plan_id"] = str(values["plan_id"])
            requests.append(ApprovalRequest.model_validate(values))
        nonces = {str(row["proof_nonce"]) for row in rows if row["proof_nonce"]}
        return tuple(requests), nonces

    async def save_approval(
        self, approval: ApprovalRequest, proof_nonce: str | None = None
    ) -> None:
        statement = text(
            """
            INSERT INTO approval_requests(
                approval_id, plan_id, plan_version, action_digest, status, proof_nonce, created_at
            ) VALUES (
                CAST(:approval_id AS uuid), CAST(:plan_id AS uuid), :plan_version,
                :action_digest, :status, :proof_nonce, :created_at
            )
            ON CONFLICT (approval_id) DO UPDATE
            SET status = EXCLUDED.status,
                proof_nonce = COALESCE(EXCLUDED.proof_nonce, approval_requests.proof_nonce)
            """
        )
        values = approval.model_dump()
        values["proof_nonce"] = proof_nonce
        async with self.engine.begin() as connection:
            await connection.execute(statement, values)

    async def load_telegram_drivers(self) -> tuple[dict[str, Any], ...]:
        statement = text(
            """
            SELECT driver_id, telegram_user_id, enrollment_digest,
                   enrollment_expires_at, enrollment_used_at, suspended_at
            FROM telegram_drivers
            """
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
            return tuple(dict(row) for row in rows)

    async def save_telegram_enrollment(
        self, driver_id: str, digest: str, expires_at: datetime
    ) -> None:
        """Persist a freshly issued (not yet used) enrollment token digest."""
        statement = text(
            """
            INSERT INTO telegram_drivers(driver_id, enrollment_digest, enrollment_expires_at)
            VALUES (:driver_id, :digest, :expires_at)
            ON CONFLICT (driver_id) DO UPDATE
            SET enrollment_digest = EXCLUDED.enrollment_digest,
                enrollment_expires_at = EXCLUDED.enrollment_expires_at,
                enrollment_used_at = NULL
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                statement, {"driver_id": driver_id, "digest": digest, "expires_at": expires_at}
            )

    async def bind_telegram_driver(self, driver_id: str, telegram_user_id: int) -> None:
        """Record a successful enrollment: bind the Telegram user and mark it used.

        Upserts rather than updating in place so a binding is still recorded
        even if the issuing row was written before persistence was enabled.
        """
        statement = text(
            """
            INSERT INTO telegram_drivers(driver_id, telegram_user_id, enrollment_used_at)
            VALUES (:driver_id, :telegram_user_id, now())
            ON CONFLICT (driver_id) DO UPDATE
            SET telegram_user_id = EXCLUDED.telegram_user_id,
                enrollment_used_at = EXCLUDED.enrollment_used_at
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                statement, {"driver_id": driver_id, "telegram_user_id": telegram_user_id}
            )

    async def suspend_telegram_driver(self, driver_id: str) -> None:
        statement = text(
            """
            INSERT INTO telegram_drivers(driver_id, suspended_at)
            VALUES (:driver_id, now())
            ON CONFLICT (driver_id) DO UPDATE SET suspended_at = EXCLUDED.suspended_at
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement, {"driver_id": driver_id})

    async def load_audit_events(self) -> tuple[AuditEvent, ...]:
        statement = text(
            """
            SELECT sequence, event_type, actor, payload, occurred_at AS timestamp,
                   previous_hash, event_hash
            FROM audit_events ORDER BY sequence
            """
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings()
            return tuple(AuditEvent.model_validate(dict(row)) for row in rows)

    async def save_audit_event(self, event: AuditEvent) -> None:
        statement = text(
            """
            INSERT INTO audit_events(
                sequence, event_type, actor, payload, occurred_at, previous_hash, event_hash
            ) VALUES (
                :sequence, :event_type, :actor, CAST(:payload AS jsonb),
                :timestamp, :previous_hash, :event_hash
            ) ON CONFLICT (sequence) DO NOTHING
            """
        )
        values = event.model_dump()
        values["payload"] = json.dumps(values["payload"], separators=(",", ":"), default=str)
        async with self.engine.begin() as connection:
            await connection.execute(statement, values)

    async def save_disruption(self, event: DisruptionEvent) -> None:
        statement = text(
            """
            INSERT INTO disruptions(event_id, scenario_id, event_type, effective_minute, payload)
            VALUES (CAST(:event_id AS uuid), :scenario_id, :event_type, :effective_minute,
                    CAST(:payload AS jsonb))
            ON CONFLICT (event_id) DO NOTHING
            """
        )
        values: dict[str, Any] = event.model_dump(mode="json")
        values["payload"] = json.dumps(values["payload"], separators=(",", ":"))
        async with self.engine.begin() as connection:
            await connection.execute(statement, values)

    async def latest_integration_snapshots(self) -> tuple[dict[str, Any], ...]:
        statement = text(
            """
            SELECT DISTINCT ON (integration, dataset)
                   integration, dataset, fetched_at, status,
                   COALESCE((payload->>'record_count')::integer,
                            jsonb_array_length(COALESCE(payload->'records', '[]'::jsonb)))
                       AS record_count
            FROM integration_snapshots
            ORDER BY integration, dataset, fetched_at DESC
            """
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings()
            return tuple(dict(row) for row in rows)

    async def synchronized_weather_traffic_days(self) -> int:
        statement = text(
            """
            WITH daily_sources AS (
                SELECT fetched_at::date AS observed_day,
                       bool_or(integration = 'LTA' AND dataset = 'speed_bands'
                               AND status = 'FRESH') AS has_traffic,
                       bool_or(integration = 'NEA' AND dataset = 'rainfall'
                               AND status = 'FRESH') AS has_weather
                FROM integration_snapshots
                GROUP BY fetched_at::date
            )
            SELECT count(*) FROM daily_sources WHERE has_traffic AND has_weather
            """
        )
        async with self.engine.connect() as connection:
            return int((await connection.execute(statement)).scalar_one())

    async def nearest_speed_context(
        self, points: tuple[tuple[str, float, float], ...]
    ) -> tuple[dict[str, float], str | None]:
        if not points:
            return {}, None
        statement = text(
            """
            WITH inputs AS (
                SELECT * FROM unnest(
                    CAST(:ids AS text[]), CAST(:lats AS double precision[]),
                    CAST(:lons AS double precision[])
                ) AS value(stop_id, lat, lon)
            )
            SELECT inputs.stop_id, nearest.minimum_speed, nearest.maximum_speed,
                   nearest.speed_band, nearest.observed_at,
                   ST_Distance(
                       nearest.midpoint,
                       ST_SetSRID(ST_MakePoint(inputs.lon, inputs.lat), 4326)::geography
                   ) AS distance_meters
            FROM inputs
            CROSS JOIN LATERAL (
                SELECT minimum_speed, maximum_speed, speed_band, observed_at, midpoint
                FROM traffic_speed_band_current
                ORDER BY midpoint <->
                    ST_SetSRID(ST_MakePoint(inputs.lon, inputs.lat), 4326)::geography
                LIMIT 1
            ) nearest
            """
        )
        params = {
            "ids": [point[0] for point in points],
            "lats": [point[1] for point in points],
            "lons": [point[2] for point in points],
        }
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement, params)).mappings().all()
        speeds = {}
        versions = []
        for row in rows:
            if row["distance_meters"] > 2000 or row["speed_band"] == 0:
                speeds[str(row["stop_id"])] = 28.0
            else:
                minimum = row["minimum_speed"] or 0
                maximum = row["maximum_speed"] or minimum
                speeds[str(row["stop_id"])] = max(5.0, (minimum + maximum) / 2)
            versions.append(row["observed_at"])
        version = max(versions).isoformat() if versions else None
        return speeds, version

    async def load_depots(self) -> tuple[Depot, ...]:
        statement = text(
            """
            SELECT depot_id, name, latitude, longitude, delivery_area,
                   operating_start_minute, operating_end_minute, cold_storage, status
            FROM depots ORDER BY depot_id
            """
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return tuple(
            Depot(
                depot_id=str(row["depot_id"]),
                name=row["name"],
                location=Coordinate(lat=row["latitude"], lon=row["longitude"]),
                delivery_area=row["delivery_area"],
                operating_start_minute=row["operating_start_minute"],
                operating_end_minute=row["operating_end_minute"],
                cold_storage=row["cold_storage"],
                status=row["status"],
            )
            for row in rows
        )

    async def load_drivers(self, available_only: bool = False) -> tuple[Driver, ...]:
        statement = text(
            """
            SELECT driver_id, name, depot_id, license_type, vocational_license,
                   certification_type, working_start_minute, working_end_minute,
                   shift_type, skill_set, availability_status
            FROM drivers
            WHERE (:available_only = FALSE OR availability_status = 'Available')
            ORDER BY driver_id
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(statement, {"available_only": available_only})
            ).mappings().all()
        return tuple(
            Driver(
                driver_id=str(row["driver_id"]),
                name=row["name"],
                depot_id=str(row["depot_id"]) if row["depot_id"] is not None else "",
                license_type=row["license_type"],
                vocational_license=row["vocational_license"],
                certification_type=row["certification_type"],
                working_start_minute=row["working_start_minute"],
                working_end_minute=row["working_end_minute"],
                shift_type=row["shift_type"],
                skill_set=split_skills(row["skill_set"]),
                availability_status=row["availability_status"],
            )
            for row in rows
        )

    async def load_orders(self, pending_only: bool = True) -> tuple[Order, ...]:
        statement = text(
            """
            SELECT order_id, delivery_address, postal_code, latitude, longitude, delivery_area,
                   window_start_minute, window_end_minute, priority_level, quantity,
                   weight_kg, volume_m3, special_handling, customer_name, contact_phone,
                   order_status
            FROM orders
            WHERE (:pending_only = FALSE OR order_status = 'Pending Dispatch')
            ORDER BY order_id
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(statement, {"pending_only": pending_only})
            ).mappings().all()
        orders = []
        for row in rows:
            handling = (row["special_handling"] or "None").strip()
            cargo_tags = () if handling in ("", "None") else (handling,)
            orders.append(
                Order(
                    order_id=str(row["order_id"]),
                    address=row["delivery_address"],
                    postal_code=row["postal_code"] or "",
                    location=Coordinate(lat=row["latitude"], lon=row["longitude"]),
                    demand=max(1, int(row["quantity"])),
                    service_seconds=300,
                    window_start_minute=row["window_start_minute"],
                    window_end_minute=row["window_end_minute"],
                    cargo_tags=cargo_tags,
                    weight_kg=row["weight_kg"],
                    volume_m3=row["volume_m3"],
                    quantity=max(1, int(row["quantity"])),
                    delivery_area=row["delivery_area"],
                    special_handling=handling or "None",
                    priority_level=int(row["priority_level"]),
                    customer_name=row["customer_name"],
                    contact_phone=row["contact_phone"],
                )
            )
        return tuple(orders)

    async def load_fleet(self, available_only: bool = True) -> tuple[Vehicle, ...]:
        """Load vehicles and pair each with an Available driver at its depot.

        The vehicle's working window is the intersection of its own availability
        window and the assigned driver's working hours. Vehicles without an
        eligible driver at the same depot are excluded, because a vehicle cannot
        run a route without a driver.
        """
        statement = text(
            """
            SELECT vehicle_id, license_plate, vehicle_type, lta_vehicle_class, fuel_type,
                   capacity_weight_kg, capacity_volume_m3, depot_id,
                   current_latitude, current_longitude,
                   availability_start_minute, availability_end_minute,
                   refrigeration_capability, vehicle_availability
            FROM vehicles
            WHERE (:available_only = FALSE OR vehicle_availability = 'Available')
            ORDER BY vehicle_id
            """
        )
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(statement, {"available_only": available_only})
            ).mappings().all()

        drivers = await self.load_drivers(available_only=available_only)
        drivers_by_depot = _pair_drivers_by_shift(drivers)

        vehicles: list[Vehicle] = []
        for row in rows:
            depot_id = str(row["depot_id"]) if row["depot_id"] is not None else ""
            pool = drivers_by_depot.get(depot_id)
            if not pool:
                continue
            driver = pool.pop(0)  # one driver per vehicle, same depot
            working_start = max(row["availability_start_minute"], driver.working_start_minute)
            working_end = min(row["availability_end_minute"], driver.working_end_minute)
            if working_start >= working_end:
                working_start, working_end = driver.working_start_minute, driver.working_end_minute
            vehicles.append(
                Vehicle(
                    vehicle_id=str(row["vehicle_id"]),
                    driver_id=driver.driver_id,
                    start=Coordinate(lat=row["current_latitude"], lon=row["current_longitude"]),
                    depot_id=depot_id,
                    capacity_weight_kg=row["capacity_weight_kg"],
                    capacity_volume_m3=row["capacity_volume_m3"],
                    vehicle_type=row["vehicle_type"],
                    license_plate=row["license_plate"],
                    lta_vehicle_class=row["lta_vehicle_class"],
                    fuel_type=row["fuel_type"],
                    refrigerated=row["refrigeration_capability"],
                    availability_start_minute=row["availability_start_minute"],
                    availability_end_minute=row["availability_end_minute"],
                    availability_status=row["vehicle_availability"],
                    working_start_minute=working_start,
                    working_end_minute=working_end,
                )
            )
        return tuple(vehicles)

    async def _fetch_records(self, table: str, order_by: str) -> tuple[dict[str, Any], ...]:
        # ``table`` and ``order_by`` are module-controlled constants, never user
        # input, so interpolation here is safe from injection.
        statement = text(f"SELECT * FROM {table} ORDER BY {order_by}")  # noqa: S608
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        records = []
        for row in rows:
            record = {key: value for key, value in dict(row).items() if key != "location"}
            records.append(record)
        return tuple(records)

    async def list_orders_raw(self) -> tuple[dict[str, Any], ...]:
        return await self._fetch_records("orders", "order_id")

    async def list_vehicles_raw(self) -> tuple[dict[str, Any], ...]:
        return await self._fetch_records("vehicles", "vehicle_id")

    async def list_drivers_raw(self) -> tuple[dict[str, Any], ...]:
        return await self._fetch_records("drivers", "driver_id")

    async def list_depots_raw(self) -> tuple[dict[str, Any], ...]:
        return await self._fetch_records("depots", "depot_id")

    async def close(self) -> None:
        await self.engine.dispose()
