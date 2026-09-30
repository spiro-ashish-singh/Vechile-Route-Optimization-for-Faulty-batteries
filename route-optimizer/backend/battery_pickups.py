"""
battery_pickups.py

Pulls pending faulty-battery pickup demand per swap station, and turns
it into the same {name, lat, lon, demand} stop shape the CVRP solver
and /solve endpoint already expect — a real-world alternative to
nairobi_example_stops() for the "collect faulty batteries from
stations" use case.

The Athena query path (faulty_battery_stations) is on hold pending an
AWS access/permissions fix, so faulty_battery_stations_from_csv() reads
the same shape of data from a CSV export instead — same columns
(country, source_location_name, source_location_id, latitude,
longitude, counting), same output stop shape either way.
"""

import csv
import os

import pandas as pd

# Defaults resolve relative to this repo (backend/.. is route-optimizer/,
# and the warehouse/ + faulty_batteries.csv files live at the repo root,
# one level above that) so this works out of the box on any machine or
# deploy target the repo is cloned onto. Every path is still overridable
# via environment variable, e.g. to point at a different export.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BATTERY_CSV_PATH = os.environ.get(
    "BATTERY_CSV_PATH", os.path.join(_REPO_ROOT, "faulty_batteries.csv")
)

# Real drop-off warehouses per country. Each file lists MULTIPLE candidate
# warehouses spread across the country (e.g. Kenya has ones in Nairobi,
# Mombasa, Kisumu, Eldoret, Kakamega) — not one central depot. cvrp_solver's
# solve_fleet treats these as multi-depot candidates: each vehicle slot is
# pinned to one candidate, and which ones actually get used is decided by
# the solver's own cost-minimization, the same way it decides fleet size.
_WAREHOUSE_FILES = {
    "Rwanda": os.environ.get(
        "RWANDA_WAREHOUSE_PATH", os.path.join(_REPO_ROOT, "warehouse", "Rwanda_wh_coordinates.csv")
    ),
    "Uganda": os.environ.get(
        "UGANDA_WAREHOUSE_PATH", os.path.join(_REPO_ROOT, "warehouse", "ug_warehouse.csv")
    ),
    "Kenya": os.environ.get(
        "KENYA_WAREHOUSE_PATH", os.path.join(_REPO_ROOT, "warehouse", "Kenya_warehouse_lat_long.xlsx")
    ),
}


def _is_missing(value):
    """True for None, blank/whitespace-only strings, and NaN floats — the
    full set of ways a spreadsheet/CSV cell can represent 'no value' across
    csv.DictReader (always strings) and pandas (may give NaN floats)."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return False


def load_warehouses(country):
    """
    Loads every real candidate drop-off warehouse for a country, from
    whichever file/format that country's data happens to be in (each one
    uses different column names, and Kenya's is an .xlsx, not a .csv).
    Returns a list of {name, lat, lon, demand: 0} dicts, test/placeholder
    rows excluded (see _is_real_station).
    """
    path = _WAREHOUSE_FILES[country]
    if path.lower().endswith(".xlsx"):
        rows = pd.read_excel(path).to_dict("records")
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

    warehouses = []
    for row in rows:
        # The ID column differs per file: Rwanda/Uganda use "Location_ID",
        # Kenya's xlsx has no such column and uses "name" for the ID instead.
        location_id = row.get("Location_ID") or row.get("name") or ""
        name = row.get("Location_Name") or row.get("warehouse") or location_id
        if not _is_real_station(name):
            continue
        if "-SS-" in str(location_id).upper():
            # A swap-station ID (pickup point), not a warehouse — landed in
            # this file by mistake (confirmed case: RW-SS-0000273 is really
            # the swap station "NYABUGOGO-KINAMBA", already collected as a
            # pickup). Filtered generically in case the same slip happened
            # for another country's warehouse file too.
            continue
        lat = row.get("latitude", row.get("Latitude"))
        lon = row.get("longitude", row.get("Longitude"))
        if _is_missing(lat) or _is_missing(lon):
            continue
        warehouses.append({"name": str(name), "lat": float(lat), "lon": float(lon), "demand": 0})
    return warehouses

# Real fuel prices per country (USD/liter). Converted to $/km using a
# PLACEHOLDER truck fuel-consumption rate (12 L/100km, typical light
# delivery truck) until the real vehicle's consumption figure is known.
FUEL_PRICE_USD_PER_LITER = {
    "Rwanda": 2.00,
    "Uganda": 1.68,
    "Kenya": 1.64,
}
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


# Rows whose name matches one of these are test/placeholder data, not a
# real pickup station — excluded outright rather than reported as "skipped"
# (skipped is reserved for real stations missing a location match).
_NON_STATION_NAME_MARKERS = ("test", "(old)")


def _is_real_station(name):
    lowered = (name or "").lower()
    return not any(marker in lowered for marker in _NON_STATION_NAME_MARKERS)


def _rows_to_stations(rows):
    stations = []
    skipped = []
    for row in rows:
        # Fall back to the location id when a station has no name, instead
        # of silently returning an empty/None name — same convention as
        # load_warehouses() uses for warehouse files with no name column.
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


def faulty_battery_stations():
    """
    Runs the faulty-battery query and returns (stations, skipped):
      - stations: one stop dict per station that has a matched location —
        {name, lat, lon, demand, source_location_id, country}
      - skipped: names/ids of stations Athena could NOT match to a
        swap_station row (missing latitude/longitude after the LEFT JOIN),
        since the routing solver has no way to place them on the map.
        Returned explicitly so pending pickups never silently vanish.
    """
    from connection import run_athena_query  # imported lazily: CSV callers shouldn't need AWS reachable
    return _rows_to_stations(run_athena_query(FAULTY_BATTERY_QUERY))


def faulty_battery_stations_from_csv(csv_path, country=None):
    """
    Same output as faulty_battery_stations(), but reads a CSV export
    (columns: country, source_location_name, source_location_id,
    latitude, longitude, counting) instead of querying Athena directly.

    country: optional filter (e.g. "Rwanda") — a truck route can't cross
    country borders here, so a real run will normally solve one country
    at a time. Pass None to get every station across all countries.
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.DictReader(f) if country is None or row["country"] == country]
    return _rows_to_stations(rows)


def stations_by_country_from_csv(csv_path):
    """Groups faulty_battery_stations_from_csv() output by country: {country: [stations]}."""
    all_stations, skipped = faulty_battery_stations_from_csv(csv_path)
    grouped = {}
    for station in all_stations:
        grouped.setdefault(station["country"], []).append(station)
    return grouped, skipped


if __name__ == "__main__":
    import sys

    csv_path = sys.argv[1] if len(sys.argv) > 1 else BATTERY_CSV_PATH
    grouped, skipped = stations_by_country_from_csv(csv_path)
    for country, stations in grouped.items():
        total_demand = sum(s["demand"] for s in stations)
        print(f"{country}: {len(stations)} stations, {total_demand} batteries pending pickup")
    if skipped:
        print(f"\n{len(skipped)} station(s) skipped (no lat/lon): {', '.join(str(s) for s in skipped)}")
