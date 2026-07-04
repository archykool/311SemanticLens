-- Postgres is the system of record (see SPEC §1.2). Everything downstream
-- (ES index, embeddings, facet mappings) is derived and rebuildable from
-- this schema alone.

CREATE TABLE IF NOT EXISTS raw_311_requests (
    unique_key                  BIGINT PRIMARY KEY,
    -- Plain TIMESTAMP, not TIMESTAMPTZ: the source gives NYC wall-clock
    -- times with no offset. Casting to TIMESTAMPTZ would silently assume
    -- the session/server timezone; staying tz-naive is the honest option.
    created_date                TIMESTAMP NOT NULL,
    closed_date                 TIMESTAMP,
    agency                      TEXT,
    agency_name                 TEXT,
    -- NYC Open Data renamed "Complaint Type"/"Descriptor" to "Problem"/
    -- "Problem Detail" in its 2025 field refresh; keeping the old names
    -- here since the rest of the spec (C1-C6) refers to them.
    complaint_type              TEXT,
    descriptor                  TEXT,
    additional_details          TEXT,
    location_type                TEXT,
    incident_zip                TEXT,
    incident_address            TEXT,
    street_name                 TEXT,
    cross_street_1              TEXT,
    cross_street_2              TEXT,
    intersection_street_1       TEXT,
    intersection_street_2       TEXT,
    address_type                TEXT,
    city                        TEXT,
    landmark                    TEXT,
    facility_type                TEXT,
    status                      TEXT,
    due_date                    TIMESTAMP,
    resolution_description      TEXT,
    resolution_updated_date     TIMESTAMP,
    community_board             TEXT,
    council_district             TEXT,
    police_precinct              TEXT,
    bbl                         TEXT,
    borough                     TEXT,
    x_coordinate                DOUBLE PRECISION,
    y_coordinate                DOUBLE PRECISION,
    channel_type                TEXT,
    park_facility_name           TEXT,
    park_borough                TEXT,
    vehicle_type                 TEXT,
    taxi_company_borough          TEXT,
    taxi_pickup_location          TEXT,
    bridge_highway_name           TEXT,
    bridge_highway_direction      TEXT,
    road_ramp                   TEXT,
    bridge_highway_segment        TEXT,
    latitude                    DOUBLE PRECISION,
    longitude                   DOUBLE PRECISION,
    -- Lineage, not part of the Socrata payload.
    source                      TEXT NOT NULL DEFAULT 'unknown',
    ingested_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- All-TEXT mirror of the Socrata/CSV columns, no constraints — COPY target
-- for bulk loads. Every field lands as raw text here (the source CSV
-- formats X/Y coordinates with thousands-separator commas, e.g. "1,046,550",
-- which isn't valid input for a numeric column) and gets cast/cleaned during
-- the INSERT ... SELECT upsert into raw_311_requests. Loading into an
-- unconstrained staging table first, then upserting, is what makes the bulk
-- job idempotent: reruns and interrupted runs just re-COPY and re-upsert,
-- never fail on a constraint mid-stream.
CREATE UNLOGGED TABLE IF NOT EXISTS staging_311_requests (
    unique_key                  TEXT,
    created_date                TEXT,
    closed_date                 TEXT,
    agency                      TEXT,
    agency_name                 TEXT,
    complaint_type              TEXT,
    descriptor                  TEXT,
    additional_details          TEXT,
    location_type                TEXT,
    incident_zip                TEXT,
    incident_address            TEXT,
    street_name                 TEXT,
    cross_street_1              TEXT,
    cross_street_2              TEXT,
    intersection_street_1       TEXT,
    intersection_street_2       TEXT,
    address_type                TEXT,
    city                        TEXT,
    landmark                    TEXT,
    facility_type                TEXT,
    status                      TEXT,
    due_date                    TEXT,
    resolution_description      TEXT,
    resolution_updated_date     TEXT,
    community_board             TEXT,
    council_district             TEXT,
    police_precinct              TEXT,
    bbl                         TEXT,
    borough                     TEXT,
    x_coordinate                TEXT,
    y_coordinate                TEXT,
    channel_type                TEXT,
    park_facility_name           TEXT,
    park_borough                TEXT,
    vehicle_type                 TEXT,
    taxi_company_borough          TEXT,
    taxi_pickup_location          TEXT,
    bridge_highway_name           TEXT,
    bridge_highway_direction      TEXT,
    road_ramp                   TEXT,
    bridge_highway_segment        TEXT,
    latitude                    TEXT,
    longitude                   TEXT,
    -- Redundant "POINT (lon lat)" string that trails both the CSV export
    -- and the Socrata JSON payload. Absorbed here so COPY can map the
    -- source columns 1:1 without per-row rewriting; never read back out.
    location_raw                TEXT
);

-- Tracks incremental-pull progress so a daily delta sync (C1 acceptance #3)
-- resumes from the last successful watermark instead of re-scanning history.
CREATE TABLE IF NOT EXISTS ingestion_watermark (
    id                  TEXT PRIMARY KEY,
    last_created_date   TIMESTAMP NOT NULL,
    rows_pulled         BIGINT NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_311_created_date ON raw_311_requests (created_date);
CREATE INDEX IF NOT EXISTS idx_raw_311_agency ON raw_311_requests (agency);
CREATE INDEX IF NOT EXISTS idx_raw_311_borough ON raw_311_requests (borough);
CREATE INDEX IF NOT EXISTS idx_raw_311_complaint_descriptor ON raw_311_requests (complaint_type, descriptor);
