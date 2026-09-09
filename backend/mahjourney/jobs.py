from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class PostgresJobCoordinator:
    """Durable schedules and atomic leases; no Redis or separate queue service."""

    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_size=2, max_overflow=0)
        self.database_dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.worker_id = f"{socket.gethostname()}:{id(self)}"

    async def ensure(self, jobs: dict[str, int]) -> None:
        statement = text(
            """
            INSERT INTO collection_jobs(job_name, interval_seconds, next_run_at)
            VALUES (:name, :seconds, now())
            ON CONFLICT (job_name) DO UPDATE
            SET interval_seconds = EXCLUDED.interval_seconds, updated_at = now()
            """
        )
        async with self.engine.begin() as connection:
            for name, seconds in jobs.items():
                await connection.execute(statement, {"name": name, "seconds": seconds})

    async def try_acquire(self, name: str, interval_seconds: int) -> bool:
        statement = text(
            """
            UPDATE collection_jobs
            SET locked_at = now(), locked_by = :worker, updated_at = now()
            WHERE job_name = :name
              AND next_run_at <= now()
              AND (locked_at IS NULL OR locked_at < now() - make_interval(secs => :lease))
            RETURNING job_name
            """
        )
        async with self.engine.begin() as connection:
            result = await connection.execute(
                statement,
                {"name": name, "worker": self.worker_id, "lease": interval_seconds * 2},
            )
            return result.scalar_one_or_none() is not None

    async def complete(
        self, name: str, interval_seconds: int, status: str, error: str = ""
    ) -> None:
        statement = text(
            """
            UPDATE collection_jobs
            SET next_run_at = now() + make_interval(secs => :seconds),
                locked_at = NULL, locked_by = NULL,
                last_status = :status, last_error = :error, updated_at = now()
            WHERE job_name = :name AND locked_by = :worker
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                statement,
                {
                    "name": name,
                    "worker": self.worker_id,
                    "seconds": interval_seconds,
                    "status": status,
                    "error": error,
                },
            )

    async def record_snapshot(self, integration: str, snapshot: Any) -> None:
        payload = snapshot.model_dump(mode="json")
        if integration == "LTA" and snapshot.dataset == "speed_bands":
            payload["records"] = []
        statement = text(
            """
            INSERT INTO integration_snapshots(
                snapshot_id, integration, dataset, fetched_at,
                response_hash, status, payload
            ) VALUES (
                CAST(:snapshot_id AS uuid), :integration, :dataset, :fetched_at,
                :response_hash, :status, CAST(:payload AS jsonb)
            ) ON CONFLICT (integration, dataset, response_hash) DO NOTHING
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                statement,
                {
                    "snapshot_id": str(snapshot.snapshot_id),
                    "integration": integration,
                    "dataset": snapshot.dataset,
                    "fetched_at": snapshot.fetched_at,
                    "response_hash": snapshot.response_hash
                    or f"empty:{datetime.now(UTC).isoformat()}",
                    "status": str(snapshot.status),
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )

    async def replace_current_speed_bands(self, snapshot: Any) -> int:
        rows = []
        for record in snapshot.records:
            try:
                rows.append(
                    (
                        str(record["LinkID"]),
                        str(record.get("RoadName", "")),
                        str(record.get("RoadCategory", "")),
                        int(record["SpeedBand"]),
                        int(record["MinimumSpeed"]) if record.get("MinimumSpeed") else None,
                        int(record["MaximumSpeed"]) if record.get("MaximumSpeed") else None,
                        float(record["StartLat"]),
                        float(record["StartLon"]),
                        float(record["EndLat"]),
                        float(record["EndLon"]),
                        UUID(str(snapshot.snapshot_id)),
                        snapshot.fetched_at,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        connection = await asyncpg.connect(self.database_dsn)
        try:
            async with connection.transaction():
                await connection.execute(
                    """
                    CREATE TEMP TABLE speed_band_stage (
                        link_id TEXT, road_name TEXT, road_category TEXT, speed_band SMALLINT,
                        minimum_speed SMALLINT, maximum_speed SMALLINT,
                        start_lat DOUBLE PRECISION, start_lon DOUBLE PRECISION,
                        end_lat DOUBLE PRECISION, end_lon DOUBLE PRECISION,
                        snapshot_id UUID, observed_at TIMESTAMPTZ
                    ) ON COMMIT DROP
                    """
                )
                await connection.copy_records_to_table("speed_band_stage", records=rows)
                await connection.execute(
                    """
                    INSERT INTO traffic_speed_band_current(
                        link_id, road_name, road_category, speed_band,
                        minimum_speed, maximum_speed, start_lat, start_lon,
                        end_lat, end_lon, midpoint, snapshot_id, observed_at
                    )
                    SELECT link_id, road_name, road_category, speed_band,
                           minimum_speed, maximum_speed, start_lat, start_lon,
                           end_lat, end_lon,
                           ST_SetSRID(ST_MakePoint(
                               (start_lon + end_lon) / 2,
                               (start_lat + end_lat) / 2
                           ), 4326)::geography,
                           snapshot_id, observed_at
                    FROM speed_band_stage
                    ON CONFLICT (link_id) DO UPDATE
                    SET road_name = EXCLUDED.road_name,
                        road_category = EXCLUDED.road_category,
                        speed_band = EXCLUDED.speed_band,
                        minimum_speed = EXCLUDED.minimum_speed,
                        maximum_speed = EXCLUDED.maximum_speed,
                        start_lat = EXCLUDED.start_lat,
                        start_lon = EXCLUDED.start_lon,
                        end_lat = EXCLUDED.end_lat,
                        end_lon = EXCLUDED.end_lon,
                        midpoint = EXCLUDED.midpoint,
                        snapshot_id = EXCLUDED.snapshot_id,
                        observed_at = EXCLUDED.observed_at
                    WHERE traffic_speed_band_current.speed_band IS DISTINCT FROM EXCLUDED.speed_band
                       OR traffic_speed_band_current.minimum_speed
                          IS DISTINCT FROM EXCLUDED.minimum_speed
                       OR traffic_speed_band_current.maximum_speed
                          IS DISTINCT FROM EXCLUDED.maximum_speed
                    """
                )
                await connection.execute(
                    """
                    DELETE FROM traffic_speed_band_current current
                    WHERE NOT EXISTS (
                        SELECT 1 FROM speed_band_stage stage WHERE stage.link_id = current.link_id
                    )
                    """
                )
        finally:
            await connection.close()
        return len(rows)

    async def compact_speed_band_history(self) -> None:
        statement = text(
            """
            UPDATE integration_snapshots
            SET payload = jsonb_set(payload, '{records}', '[]'::jsonb, true)
            WHERE integration = 'LTA' AND dataset = 'speed_bands'
              AND jsonb_array_length(COALESCE(payload->'records', '[]'::jsonb)) > 0
            """
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def close(self) -> None:
        await self.engine.dispose()
