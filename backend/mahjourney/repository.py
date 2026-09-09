from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .domain import (
    ApprovalRequest,
    AuditEvent,
    ConversationMessage,
    DisruptionEvent,
    MemoryItem,
    PlanVersion,
)


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

    async def close(self) -> None:
        await self.engine.dispose()
