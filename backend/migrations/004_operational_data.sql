-- Operational fleet data imported from the Singapore logistics workbook.
-- These tables replace the hardcoded synthetic fixtures as the source of
-- orders, vehicles, drivers and depots used by the planning engine.

CREATE TABLE IF NOT EXISTS depots (
    depot_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    location GEOGRAPHY(POINT, 4326) NOT NULL,
    delivery_area TEXT NOT NULL DEFAULT '',
    operating_start_minute INTEGER NOT NULL DEFAULT 0,
    operating_end_minute INTEGER NOT NULL DEFAULT 1439,
    storage_capacity_m3 DOUBLE PRECISION,
    vehicle_capacity INTEGER,
    cold_storage BOOLEAN NOT NULL DEFAULT FALSE,
    loading_bays INTEGER,
    status TEXT NOT NULL DEFAULT 'Operational'
);
CREATE INDEX IF NOT EXISTS ix_depots_location ON depots USING GIST(location);

CREATE TABLE IF NOT EXISTS drivers (
    driver_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    depot_id TEXT REFERENCES depots(depot_id),
    license_type TEXT NOT NULL DEFAULT '',
    vocational_license TEXT NOT NULL DEFAULT '',
    certification_type TEXT NOT NULL DEFAULT '',
    working_start_minute INTEGER NOT NULL DEFAULT 480,
    working_end_minute INTEGER NOT NULL DEFAULT 1080,
    shift_type TEXT NOT NULL DEFAULT '',
    home_depot TEXT NOT NULL DEFAULT '',
    skill_set TEXT NOT NULL DEFAULT '',
    availability_status TEXT NOT NULL DEFAULT 'Available'
);
CREATE INDEX IF NOT EXISTS ix_drivers_depot ON drivers(depot_id);
CREATE INDEX IF NOT EXISTS ix_drivers_availability ON drivers(availability_status);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    license_plate TEXT NOT NULL DEFAULT '',
    vehicle_type TEXT NOT NULL DEFAULT '',
    vehicle_make TEXT NOT NULL DEFAULT '',
    vehicle_model TEXT NOT NULL DEFAULT '',
    vehicle_year INTEGER,
    capacity_weight_kg DOUBLE PRECISION NOT NULL,
    capacity_volume_m3 DOUBLE PRECISION NOT NULL,
    lta_vehicle_class TEXT NOT NULL DEFAULT '',
    fuel_type TEXT NOT NULL DEFAULT '',
    fuel_consumption_l_per_100km DOUBLE PRECISION,
    iu_status TEXT NOT NULL DEFAULT '',
    current_depot TEXT NOT NULL DEFAULT '',
    depot_id TEXT REFERENCES depots(depot_id),
    current_latitude DOUBLE PRECISION NOT NULL,
    current_longitude DOUBLE PRECISION NOT NULL,
    location GEOGRAPHY(POINT, 4326) NOT NULL,
    availability_start_minute INTEGER NOT NULL DEFAULT 0,
    availability_end_minute INTEGER NOT NULL DEFAULT 1439,
    special_equipment TEXT NOT NULL DEFAULT '',
    refrigeration_capability BOOLEAN NOT NULL DEFAULT FALSE,
    vehicle_availability TEXT NOT NULL DEFAULT 'Available',
    vehicle_status TEXT NOT NULL DEFAULT 'In Fleet'
);
CREATE INDEX IF NOT EXISTS ix_vehicles_depot ON vehicles(depot_id);
CREATE INDEX IF NOT EXISTS ix_vehicles_availability ON vehicles(vehicle_availability);
CREATE INDEX IF NOT EXISTS ix_vehicles_location ON vehicles USING GIST(location);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    location GEOGRAPHY(POINT, 4326) NOT NULL,
    delivery_area TEXT NOT NULL DEFAULT '',
    window_start_minute INTEGER NOT NULL DEFAULT 480,
    window_end_minute INTEGER NOT NULL DEFAULT 1080,
    order_date DATE,
    requested_delivery_date DATE,
    shipping_method TEXT NOT NULL DEFAULT '',
    priority_level INTEGER NOT NULL DEFAULT 3,
    quantity INTEGER NOT NULL DEFAULT 1,
    weight_kg DOUBLE PRECISION NOT NULL DEFAULT 0,
    volume_m3 DOUBLE PRECISION NOT NULL DEFAULT 0,
    special_handling TEXT NOT NULL DEFAULT 'None',
    delivery_type TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    special_instructions TEXT NOT NULL DEFAULT '',
    order_status TEXT NOT NULL DEFAULT 'Pending Dispatch'
);
CREATE INDEX IF NOT EXISTS ix_orders_area ON orders(delivery_area);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(order_status);
CREATE INDEX IF NOT EXISTS ix_orders_location ON orders USING GIST(location);
