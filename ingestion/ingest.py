"""C1 ingestion: NYC 311 -> Postgres (system of record).

Two modes, both idempotent (see sql/001_schema.sql for why staging + upsert
makes reruns safe):

  bulk-csv      One-shot load of a local CSV export (the already-pulled
                Phase 1 "catch-basin slice": DSNY/HPD/DEP/DOT/NYPD/DOHMH/DPR,
                calendar year 2025, ~766K rows). Streams the file straight
                into staging via COPY, then upserts.

  incremental   Daily-delta mode against the live Socrata SODA API, using
                the same agency filter, resuming from ingestion_watermark
                so a rerun after a failed pull doesn't redo the whole day.

Usage:
    python ingest.py bulk-csv --csv /data/311_2025_CloggedBasin.csv
    python ingest.py incremental
"""

import argparse
import csv
import io
import os
import sys
from datetime import datetime, timezone

import psycopg
import requests

SOCRATA_DATASET = "erm2-nwe9"  # "311 Service Requests from 2010 to Present"
SOCRATA_BASE = f"https://data.cityofnewyork.us/resource/{SOCRATA_DATASET}.json"
SOCRATA_PAGE_LIMIT = 50000  # SODA API max $limit per request

# Matches the agency scope already pulled for the local CSV bootstrap — see
# SPEC C2: catch-basin problems surface under many labels across these
# agencies, and Q5 (co-occurrence) needs that cross-agency breadth. Keeping
# incremental pulls on the same filter keeps the corpus consistent.
AGENCIES = ["DSNY", "HPD", "DEP", "DOT", "NYPD", "DOHMH", "DPR"]
PHASE1_START = datetime(2025, 1, 1)

WATERMARK_ID = "socrata_311_pull"

# Staging column order — must match the CSV export's column order exactly,
# since COPY ... HEADER only skips the header line, it does not match by
# name. See the header-count guard in bulk_csv() for a cheap safety check.
STAGING_COLUMNS = [
    "unique_key", "created_date", "closed_date", "agency", "agency_name",
    "complaint_type", "descriptor", "additional_details", "location_type",
    "incident_zip", "incident_address", "street_name", "cross_street_1",
    "cross_street_2", "intersection_street_1", "intersection_street_2",
    "address_type", "city", "landmark", "facility_type", "status",
    "due_date", "resolution_description", "resolution_updated_date",
    "community_board", "council_district", "police_precinct", "bbl",
    "borough", "x_coordinate", "y_coordinate", "channel_type",
    "park_facility_name", "park_borough", "vehicle_type",
    "taxi_company_borough", "taxi_pickup_location", "bridge_highway_name",
    "bridge_highway_direction", "road_ramp", "bridge_highway_segment",
    "latitude", "longitude", "location_raw",
]

# Socrata JSON field name -> our staging column name (only fields we keep;
# absent keys become NULL). x/y coordinates come from *_state_plane and,
# unlike the CSV export, are plain numeric strings with no thousands commas.
SOCRATA_FIELD_MAP = {
    "unique_key": "unique_key",
    "created_date": "created_date",
    "closed_date": "closed_date",
    "agency": "agency",
    "agency_name": "agency_name",
    "complaint_type": "complaint_type",
    "descriptor": "descriptor",
    "additional_details": "additional_details",
    "location_type": "location_type",
    "incident_zip": "incident_zip",
    "incident_address": "incident_address",
    "street_name": "street_name",
    "cross_street_1": "cross_street_1",
    "cross_street_2": "cross_street_2",
    "intersection_street_1": "intersection_street_1",
    "intersection_street_2": "intersection_street_2",
    "address_type": "address_type",
    "city": "city",
    "landmark": "landmark",
    "facility_type": "facility_type",
    "status": "status",
    "due_date": "due_date",
    "resolution_description": "resolution_description",
    "resolution_action_updated_date": "resolution_updated_date",
    "community_board": "community_board",
    "council_district": "council_district",
    "police_precinct": "police_precinct",
    "bbl": "bbl",
    "borough": "borough",
    "x_coordinate_state_plane": "x_coordinate",
    "y_coordinate_state_plane": "y_coordinate",
    "open_data_channel_type": "channel_type",
    "park_facility_name": "park_facility_name",
    "park_borough": "park_borough",
    "vehicle_type": "vehicle_type",
    "taxi_company_borough": "taxi_company_borough",
    "taxi_pick_up_location": "taxi_pickup_location",
    "bridge_highway_name": "bridge_highway_name",
    "bridge_highway_direction": "bridge_highway_direction",
    "road_ramp": "road_ramp",
    "bridge_highway_segment": "bridge_highway_segment",
    "latitude": "latitude",
    "longitude": "longitude",
}

UPSERT_SQL_PATH = os.path.join(os.path.dirname(__file__), "upsert_from_staging.sql")


def get_conn():
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
        user=os.environ.get("POSTGRES_USER", "semanticlens"),
        password=os.environ.get("POSTGRES_PASSWORD", "changeme"),
    )


def run_upsert(cur, source_tag):
    with open(UPSERT_SQL_PATH, encoding="utf-8") as f:
        cur.execute(f.read(), {"source": source_tag})


def bulk_csv(csv_path):
    with open(csv_path, encoding="utf-8", newline="") as f:
        header = next(csv.reader(f))
    if len(header) != len(STAGING_COLUMNS):
        sys.exit(
            f"CSV header has {len(header)} columns, expected {len(STAGING_COLUMNS)}. "
            "NYC's export format may have changed — check column order against "
            "STAGING_COLUMNS in ingest.py before loading."
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE staging_311_requests")
            with open(csv_path, "rb") as f, cur.copy(
                "COPY staging_311_requests FROM STDIN WITH (FORMAT csv, HEADER true)"
            ) as copy:
                while chunk := f.read(1024 * 1024):
                    copy.write(chunk)

            staged = cur.execute("SELECT count(*) FROM staging_311_requests").fetchone()[0]
            print(f"staged {staged} rows from {csv_path}")

            run_upsert(cur, source_tag="csv_bootstrap")
            cur.execute("TRUNCATE staging_311_requests")

            total = cur.execute("SELECT count(*) FROM raw_311_requests").fetchone()[0]
            print(f"raw_311_requests row count after upsert: {total}")


def fetch_socrata_page(where_clause, offset, app_token):
    headers = {"X-App-Token": app_token} if app_token else {}
    params = {
        "$where": where_clause,
        "$order": "created_date ASC",
        "$limit": SOCRATA_PAGE_LIMIT,
        "$offset": offset,
    }
    resp = requests.get(SOCRATA_BASE, params=params, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()


def stage_socrata_batch(cur, rows):
    values = []
    for row in rows:
        record = {col: None for col in STAGING_COLUMNS}
        for socrata_key, col in SOCRATA_FIELD_MAP.items():
            if socrata_key in row:
                record[col] = row[socrata_key]
        values.append(tuple(record[col] for col in STAGING_COLUMNS))

    placeholders = ", ".join(["%s"] * len(STAGING_COLUMNS))
    cur.executemany(
        f"INSERT INTO staging_311_requests ({', '.join(STAGING_COLUMNS)}) "
        f"VALUES ({placeholders})",
        values,
    )


def incremental(app_token):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                "SELECT last_created_date FROM ingestion_watermark WHERE id = %s",
                (WATERMARK_ID,),
            ).fetchone()
            since = row[0] if row else PHASE1_START

            agency_list = ", ".join(f"'{a}'" for a in AGENCIES)
            where_clause = f"created_date > '{since.isoformat()}' AND agency IN ({agency_list})"

            cur.execute("TRUNCATE staging_311_requests")
            offset = 0
            total_pulled = 0
            max_created = since
            while True:
                page = fetch_socrata_page(where_clause, offset, app_token)
                if not page:
                    break
                stage_socrata_batch(cur, page)
                offset += len(page)
                total_pulled += len(page)
                page_max = max(datetime.fromisoformat(r["created_date"]) for r in page)
                max_created = max(max_created, page_max)
                if len(page) < SOCRATA_PAGE_LIMIT:
                    break

            if total_pulled == 0:
                print(f"no new rows since {since.isoformat()}")
                return

            run_upsert(cur, source_tag="socrata_incremental")
            cur.execute("TRUNCATE staging_311_requests")

            cur.execute(
                """
                INSERT INTO ingestion_watermark (id, last_created_date, rows_pulled)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_created_date = EXCLUDED.last_created_date,
                    rows_pulled = ingestion_watermark.rows_pulled + EXCLUDED.rows_pulled,
                    updated_at = now()
                """,
                (WATERMARK_ID, max_created, total_pulled),
            )
            print(f"pulled {total_pulled} rows, watermark advanced to {max_created.isoformat()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    bulk_parser = sub.add_parser("bulk-csv", help="One-shot load from a local CSV export")
    bulk_parser.add_argument("--csv", required=True, help="Path to the CSV file")

    sub.add_parser("incremental", help="Daily-delta pull from the Socrata API")

    args = parser.parse_args()

    if args.mode == "bulk-csv":
        bulk_csv(args.csv)
    elif args.mode == "incremental":
        incremental(os.environ.get("SOCRATA_APP_TOKEN") or None)


if __name__ == "__main__":
    main()
