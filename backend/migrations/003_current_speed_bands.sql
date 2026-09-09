CREATE TABLE IF NOT EXISTS traffic_speed_band_current (
    link_id TEXT PRIMARY KEY,
    road_name TEXT NOT NULL,
    road_category TEXT NOT NULL,
    speed_band SMALLINT NOT NULL CHECK (speed_band BETWEEN 0 AND 8),
    minimum_speed SMALLINT,
    maximum_speed SMALLINT,
    start_lat DOUBLE PRECISION NOT NULL,
    start_lon DOUBLE PRECISION NOT NULL,
    end_lat DOUBLE PRECISION NOT NULL,
    end_lon DOUBLE PRECISION NOT NULL,
    midpoint GEOGRAPHY(POINT, 4326) NOT NULL,
    snapshot_id UUID NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_speed_band_current_midpoint
    ON traffic_speed_band_current USING GIST(midpoint);
CREATE INDEX IF NOT EXISTS ix_speed_band_current_observed
    ON traffic_speed_band_current(observed_at);
