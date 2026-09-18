CREATE TABLE wards (
    wardnum INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    geometry GEOMETRY(MultiPolygon, 4326),
    elevation_mean NUMERIC,
    river_distance_m NUMERIC,
    dam_discharge_incident_count INTEGER
);

CREATE TABLE rainfall_daily (
    date DATE PRIMARY KEY,
    rainfall_mm NUMERIC
);

SELECT create_hypertable('rainfall_daily', 'date');

CREATE TABLE dam_discharge_events (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    cusecs NUMERIC,
    locality TEXT,
    ward_id INTEGER REFERENCES wards(wardnum)
);