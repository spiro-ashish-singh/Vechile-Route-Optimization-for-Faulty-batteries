"""
battery_stations.py

Netlify Function serving GET /battery-stations?country=... — a lighter
standalone reimplementation of backend/battery_pickups.py's endpoint of
the same name, kept dependency-light (boto3 only, no pandas/fastapi) so
the deployed bundle stays under Netlify's function size limit.

Data files (faulty_batteries.csv + each country's warehouse CSV) are
bundled in battery_stations_data/ next to this file rather than read
from the backend's local dev machine paths.

AWS credentials come from this site's environment variables (Netlify
dashboard -> Site configuration -> Environment variables), read via
os.environ exactly like backend/connection.py does — same variables:
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, ATHENA_WORKGROUP,
ATHENA_OUTPUT_S3.
"""

import csv
import json
import math
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "battery_stations_data")

BATTERY_CSV_PATH = os.path.join(DATA_DIR, "faulty_batteries.csv")
_WAREHOUSE_FILES = {
    "Rwanda": os.path.join(DATA_DIR, "Rwanda_wh_coordinates.csv"),
    "Uganda": os.path.join(DATA_DIR, "ug_warehouse.csv"),
    "Kenya": os.path.join(DATA_DIR, "Kenya_warehouse_lat_long.csv"),
}

FUEL_PRICE_USD_PER_LITER = {"Rwanda": 2.00, "Uganda": 1.68, "Kenya": 1.64}
FUEL_CONSUMPTION_L_PER_KM = 0.12

FAULTY_BATTERY_QUERY = """
SELECT
    f.country,
    f.source_location_name,
    f.source_location_id,
    ss.latitude,
    ss.longitude,
    COUNT(*) AS counting
FROM "Data-Athena-prod"."landing_db"."faulty_tagged_assets" f
LEFT JOIN "AwsDataCatalog"."prod_landing_db"."atlas_swap_station" ss
    ON f.source_location_id = ss.swap_station_id
WHERE current_status = 'APPROVED_FOR_MOVEMENT'
    AND collected_or_pending = 'pending'
GROUP BY f.country, f.source_location_id, f.source_location_name,
    ss.latitude, ss.longitude
ORDER BY counting DESC
"""

_NON_STATION_NAME_MARKERS = ("test", "(old)")


def _is_missing(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _is_real_station(name):
    lowered = (name or "").lower()
    return not any(marker in lowered for marker in _NON_STATION_NAME_MARKERS)


def load_warehouses(country):
    path = _WAREHOUSE_FILES[country]
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    warehouses = []
    for row in rows:
        location_id = row.get("Location_ID") or row.get("name") or ""
        name = row.get("Location_Name") or row.get("warehouse") or location_id
        if not _is_real_station(name):
            continue
        if "-SS-" in str(location_id).upper():
            continue
        lat = row.get("latitude", row.get("Latitude"))
        lon = row.get("longitude", row.get("Longitude"))
        if _is_missing(lat) or _is_missing(lon):
            continue
        warehouses.append({"name": str(name), "lat": float(lat), "lon": float(lon), "demand": 0})
    return warehouses


def _rows_to_stations(rows):
    stations = []
    skipped = []
    for row in rows:
        name = row.get("source_location_name") or row.get("source_location_id")
        if not _is_real_station(name):
            continue
        if _is_missing(row.get("latitude")) or _is_missing(row.get("longitude")):
            skipped.append(name)
            continue
        stations.append({
            "name": name,
            "lat": float(row["latitude"]),
            "lon": float(row["longitude"]),
            "demand": int(row["counting"]),
            "source_location_id": row["source_location_id"],
            "country": row["country"],
        })
    return stations, skipped


def faulty_battery_stations_from_csv(csv_path, country=None):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.DictReader(f) if country is None or row["country"] == country]
    return _rows_to_stations(rows)


def _run_athena_query(query, timeout_seconds=25, poll_interval=1.0):
    import boto3

    client = boto3.client(
        "athena",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
    )

    start_kwargs = {"QueryString": query, "WorkGroup": os.environ.get("ATHENA_WORKGROUP", "primary")}
    output_s3 = os.environ.get("ATHENA_OUTPUT_S3", "")
    if output_s3:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": output_s3}

    query_execution_id = client.start_query_execution(**start_kwargs)["QueryExecutionId"]

    deadline = time.time() + timeout_seconds
    while True:
        status = client.get_query_execution(QueryExecutionId=query_execution_id)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "no reason given")
            raise RuntimeError(f"Athena query {state.lower()}: {reason}")
        if time.time() > deadline:
            raise TimeoutError(f"Athena query still {state} after {timeout_seconds}s")
        time.sleep(poll_interval)

    rows = []
    columns = None
    paginator = client.get_paginator("get_query_results")
    for page in paginator.paginate(QueryExecutionId=query_execution_id):
        result_rows = page["ResultSet"]["Rows"]
        if columns is None:
            columns = [cell.get("VarCharValue") for cell in result_rows[0]["Data"]]
            result_rows = result_rows[1:]
        for row in result_rows:
            values = [cell.get("VarCharValue") for cell in row["Data"]]
            rows.append(dict(zip(columns, values)))
    return rows


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    country = params.get("country", "Rwanda")

    if country not in FUEL_PRICE_USD_PER_LITER:
        return _response(400, {"detail": f"Unknown country '{country}'. Choose one of: {', '.join(FUEL_PRICE_USD_PER_LITER)}"})

    data_source = "athena"
    try:
        all_stations, skipped = _rows_to_stations(_run_athena_query(FAULTY_BATTERY_QUERY))
        stations = [s for s in all_stations if s["country"] == country]
    except Exception as e:
        data_source = f"csv (athena unavailable: {e})"
        stations, skipped = faulty_battery_stations_from_csv(BATTERY_CSV_PATH, country=country)

    depot_stops = [
        {**w, "small_boxes": 0, "mid_boxes": 0, "large_boxes": 0, "is_depot": True}
        for w in load_warehouses(country)
    ]
    stops = depot_stops + [
        {**s, "small_boxes": 0, "mid_boxes": s["demand"], "large_boxes": 0}
        for s in stations
    ]
    road_cost_per_km = FUEL_PRICE_USD_PER_LITER[country] * FUEL_CONSUMPTION_L_PER_KM

    return _response(200, {
        "data_source": data_source,
        "stops": stops,
        "skipped": skipped,
        "country": country,
        "road_cost_per_km": road_cost_per_km,
        "warehouse_count": len(depot_stops),
    })
