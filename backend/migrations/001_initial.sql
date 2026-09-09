CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id UUID NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL,
    source_data_version TEXT NOT NULL,
    objective_cost DOUBLE PRECISION NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, version)
);

CREATE TABLE IF NOT EXISTS integration_snapshots (
    snapshot_id UUID PRIMARY KEY,
    integration TEXT NOT NULL,
    dataset TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    response_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    payload JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshot_hash ON integration_snapshots(integration, dataset, response_hash);

CREATE TABLE IF NOT EXISTS collection_jobs (
    job_name TEXT PRIMARY KEY,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    next_run_at TIMESTAMPTZ NOT NULL,
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    last_status TEXT,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS disruptions (
    event_id UUID PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    effective_minute INTEGER NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id UUID PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(1536),
    status TEXT NOT NULL,
    trust_label TEXT NOT NULL,
    supersedes_id UUID REFERENCES memory_items(memory_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id UUID PRIMARY KEY,
    plan_id UUID NOT NULL,
    plan_version INTEGER NOT NULL,
    action_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    proof_nonce TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (plan_id, plan_version) REFERENCES plan_versions(plan_id, version)
);

CREATE TABLE IF NOT EXISTS telegram_drivers (
    driver_id TEXT PRIMARY KEY,
    telegram_user_id BIGINT UNIQUE,
    enrollment_digest TEXT UNIQUE,
    enrollment_expires_at TIMESTAMPTZ,
    enrollment_used_at TIMESTAMPTZ,
    suspended_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS route_weather_features (
    route_leg_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    midpoint GEOGRAPHY(POINT, 4326) NOT NULL,
    rainfall_station_id TEXT,
    forecast_area TEXT,
    wet_or_dry TEXT NOT NULL,
    rain_expected BOOLEAN NOT NULL,
    source_snapshot_ids UUID[] NOT NULL,
    PRIMARY KEY (route_leg_id, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_route_weather_midpoint ON route_weather_features USING GIST(midpoint);
