"""
app.py

This is the WEB SERVER — the bridge between your browser (the map UI)
and the Python optimization logic we already built and tested
(cvrp_solver.py, stops_and_cost.py).

HOW IT WORKS (concept, since this might be new to you):
A web server listens for HTTP requests (the same kind your browser
sends when you visit any website) and sends back responses. FastAPI
is a Python library that makes this easy: you write a normal Python
function, put a decorator like `@app.get("/solve")` above it, and
FastAPI automatically turns it into something a browser/JavaScript
can call over the network.

THE FLOW:
  1. You open index.html in your browser (the map).
  2. JavaScript in that page sends a request to this server, e.g.
     "solve this problem with these stops and this capacity."
  3. This file runs your existing solve_cvrp() function.
  4. It sends the result back as JSON (a text format JavaScript can
     easily read) — the frontend then draws the route on the map.

RUNNING THIS:
  cd backend
  pip install fastapi uvicorn
  uvicorn app:app --reload --port 8000

Then open http://localhost:8000/docs in a browser — FastAPI
auto-generates an interactive API tester there, which is a great way
to try this out even before the frontend map exists.
"""

import csv
import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, confloat
from typing import List, Optional

from stops_and_cost import (
    haversine_cost_matrix, road_network_cost_matrix, osrm_cost_and_time_matrices, tomtom_cost_and_time_matrices,
    distance_km_from_matrix_cost, fuel_cost_from_matrix_cost,
)
from cvrp_solver import solve_cvrp, solve_fleet
from regions import LEVELS, boundary_path, tag_regions


app = FastAPI(title="Truck Route + Capacity Optimizer")

# CORS = "Cross-Origin Resource Sharing". Browsers block a webpage from
# calling a server on a different address/port unless the server
# explicitly allows it. Since our frontend (e.g. served from a file or
# a different port) needs to call this backend (port 8000), we allow
# all origins here. In a real production app you'd restrict this to
# your actual frontend's address, but for local development this is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/response shapes
# ---------------------------------------------------------------------------
# Pydantic models describe exactly what JSON shape we expect coming in,
# and FastAPI uses this to auto-validate requests (e.g. reject a request
# missing "lat") and auto-generate documentation.

class Stop(BaseModel):
    name: str
    source_location_id: Optional[str] = Field(None, description="Station/warehouse code, echoed back per stop in truck_routes")
    country: Optional[str] = None
    region: Optional[str] = None
    district: Optional[str] = None
    working_batteries: Optional[int] = Field(None, ge=0, description="Working batteries at this station, for the faulty % in the report")
    lat: float
    lon: float
    demand: Optional[int] = Field(None, ge=0, description="Legacy total box count; calculated from size quantities when supplied")
    small_boxes: int = Field(0, ge=0)
    mid_boxes: int = Field(0, ge=0)
    large_boxes: int = Field(0, ge=0)
    drop_lat: Optional[float] = None
    drop_lon: Optional[float] = None
    is_depot: bool = Field(False, description="Marks this stop as a depot/warehouse candidate. Multiple depot-flagged stops enable multi-depot routing — each vehicle is pinned to one, and the solver decides which depots actually get used. If none are flagged, stops[0] is treated as the sole depot (legacy behavior).")


class SolveRequest(BaseModel):
    stops: List[Stop] = Field(..., min_length=2, description="First stop MUST be the depot (demand=0)")
    shipment_mode: str = Field("same", pattern="^(same|different)$")
    vehicle_capacity: int = Field(500, gt=0)
    target_fill_ratio: float = Field(0.90, ge=0, le=1)
    prefer_more_stops: bool = True
    road_cost_per_km: float = Field(8.0, ge=0, description="Fuel/road cost per km, added to distance")
    routing_algorithm: str = Field("tomtom", pattern="^(tomtom|osrm)$")
    road_place: str = Field("Nairobi, Kenya", min_length=1)
    max_trucks: Optional[int] = Field(None, ge=1, le=1000, description="Optional safety limit; fleet size is normally calculated automatically")
    max_driving_hours: float = Field(8.0, gt=0, le=24, description="Maximum driving time allowed per truck per day")
    max_trip_days: int = Field(1, ge=1, le=14, description="Max days a single truck's trip may span before it must return to depot (driving budget = max_driving_hours * max_trip_days)")
    toll_taxes: List[confloat(ge=0)] = Field(default_factory=list, description="Fixed tax amount for each toll used on the route")
    excluded_regions: List[str] = Field(default_factory=list, description="Level-1 areas (county/province/region); stops whose region is listed are removed before routing")
    excluded_districts: List[str] = Field(default_factory=list, description="Level-2 areas (sub-county/district); stops whose district is listed are removed before routing")


class SolveResponse(BaseModel):
    visit_order: List[int]
    visit_order_names: List[str]
    dropped_stops: List[int]
    dropped_stop_names: List[str]
    total_cartons_picked: int
    fill_ratio: float
    meets_fill_target: bool
    num_stops_served: int
    total_cost: float
    fuel_cost: float
    toll_tax_total: float
    total_operating_cost: float
    routing_algorithm: str
    trucks_needed: int
    truck_routes: List[dict]
    route_coordinates: List[dict]  # [{lat, lon}, ...] in visiting order, for drawing on the map
    dropped_stop_details: List[dict]
    region_excluded_stops: List[dict]  # stops removed up front because they lie in an excluded region/district


# ---------------------------------------------------------------------------
# Routes (API endpoints)
# ---------------------------------------------------------------------------
@app.get("/")
def health_check():
    """Simple endpoint to confirm the server is running."""
    return {"status": "ok", "message": "Truck route optimizer API is running."}


@app.get("/example-stops")
def get_example_stops():
    """
    Returns the built-in Nairobi example stops, so the frontend can
    load a ready-made scenario with one click instead of the user
    having to manually place every stop on the map first.
    """
    from stops_and_cost import nairobi_example_stops
    return {"stops": nairobi_example_stops()}


class RouteGeometryRequest(BaseModel):
    coordinates: List[List[float]] = Field(..., min_length=2, description="[[lat, lon], ...] in visiting order")


@app.post("/route-geometry")
def route_geometry(request: RouteGeometryRequest):
    """
    Traces the real road-following path through a truck's stops, via
    OSRM. Proxied through the backend rather than called directly from
    the browser — OSRM's /route endpoint rejects direct browser requests
    with a CORS error, even though the same request works fine server-side.
    """
    from stops_and_cost import osrm_route_geometry
    try:
        geometry = osrm_route_geometry([(c[0], c[1]) for c in request.coordinates])
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Road geometry service unavailable: {e}")
    return {"geometry": geometry}


def attach_working_batteries(stops):
    """
    Adds working_batteries (from Athena's daily station utilisation) to each
    station stop, matched on station code. Returns a short description of
    the outcome; on failure every station keeps working_batteries=None so
    the report leaves those columns blank instead of guessing.
    """
    from battery_pickups import working_batteries_by_station

    try:
        working = working_batteries_by_station()
    except Exception as e:
        for stop in stops:
            stop["working_batteries"] = None
        return f"unavailable ({e})"
    matched = 0
    for stop in stops:
        stop["working_batteries"] = None if stop.get("is_depot") else working.get(stop.get("source_location_id"))
        matched += stop["working_batteries"] is not None
    return f"athena ({matched} station(s) matched)"


@app.get("/battery-stations")
def get_battery_stations(country: str = "Rwanda"):
    """
    Returns faulty-battery pickup stations for one country (from the CSV
    export, via battery_pickups.py), in the same {name, lat, lon, demand}
    shape /example-stops uses, with a depot placeholder prepended as
    stop 0 — so the frontend can load it exactly like the Nairobi example.

    Also returns the placeholder->real fuel cost per km for this country,
    so the frontend can pre-fill the fuel-cost field with a real number
    instead of the generic default.

    Tries Athena (live data) first; falls back to the CSV export if
    Athena is unreachable or misconfigured, same fallback pattern as
    TomTom -> OSRM for routing.
    """
    from battery_pickups import (
        faulty_battery_stations,
        faulty_battery_stations_from_csv,
        load_warehouses,
        BATTERY_CSV_PATH,
        FUEL_PRICE_USD_PER_LITER,
        FUEL_CONSUMPTION_L_PER_KM,
    )

    if country not in FUEL_PRICE_USD_PER_LITER:
        raise HTTPException(status_code=400, detail=f"Unknown country '{country}'. Choose one of: {', '.join(FUEL_PRICE_USD_PER_LITER)}")

    data_source = "athena"
    try:
        all_stations, skipped = faulty_battery_stations()
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
    tag_regions(stops, country)
    working_data_source = attach_working_batteries(stops)
    road_cost_per_km = FUEL_PRICE_USD_PER_LITER[country] * FUEL_CONSUMPTION_L_PER_KM

    return {
        "data_source": data_source,
        "stops": stops,
        "skipped": skipped,
        "country": country,
        "road_cost_per_km": road_cost_per_km,
        "warehouse_count": len(depot_stops),
        "working_data_source": working_data_source,
    }


@app.post("/battery-stations/upload")
async def upload_battery_stations(
    country: str = Form(...),
    stations_csv: UploadFile = File(..., description="Columns: country, source_location_name, source_location_id, latitude, longitude, counting"),
    warehouse_csv: Optional[UploadFile] = File(None, description="Optional. Columns: Location_ID/name, Location_Name/warehouse, latitude, longitude. Falls back to this country's built-in warehouse file if omitted."),
):
    """
    Same response shape as GET /battery-stations, but built from
    user-uploaded CSV files instead of Athena or the bundled CSV export —
    for feeding in a fresher or different dataset without redeploying.
    """
    from battery_pickups import (
        faulty_battery_stations_from_csv_text,
        load_warehouses,
        load_warehouses_from_csv_text,
        FUEL_PRICE_USD_PER_LITER,
        FUEL_CONSUMPTION_L_PER_KM,
    )

    if country not in FUEL_PRICE_USD_PER_LITER:
        raise HTTPException(status_code=400, detail=f"Unknown country '{country}'. Choose one of: {', '.join(FUEL_PRICE_USD_PER_LITER)}")

    try:
        stations_text = (await stations_csv.read()).decode("utf-8-sig")
        stations, skipped = faulty_battery_stations_from_csv_text(stations_text, country=country)

        if warehouse_csv is not None:
            warehouse_text = (await warehouse_csv.read()).decode("utf-8-sig")
            warehouses = load_warehouses_from_csv_text(warehouse_text)
        else:
            warehouses = load_warehouses(country)
    except (csv.Error, UnicodeDecodeError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"Could not parse uploaded CSV: {e}")

    depot_stops = [
        {**w, "small_boxes": 0, "mid_boxes": 0, "large_boxes": 0, "is_depot": True}
        for w in warehouses
    ]
    stops = depot_stops + [
        {**s, "small_boxes": 0, "mid_boxes": s["demand"], "large_boxes": 0}
        for s in stations
    ]
    tag_regions(stops, country)
    working_data_source = attach_working_batteries(stops)
    road_cost_per_km = FUEL_PRICE_USD_PER_LITER[country] * FUEL_CONSUMPTION_L_PER_KM

    return {
        "data_source": "csv-upload",
        "stops": stops,
        "skipped": skipped,
        "country": country,
        "road_cost_per_km": road_cost_per_km,
        "warehouse_count": len(depot_stops),
        "working_data_source": working_data_source,
    }


@app.get("/regions/{country}/{level}")
def get_region_boundaries(country: str, level: int):
    """GeoJSON outlines of a country's level-1 (region) or level-2 (district) areas, for shading excluded areas on the map."""
    if not country.isalpha() or level not in LEVELS or not os.path.exists(boundary_path(country, level)):
        raise HTTPException(status_code=404, detail=f"No level-{level} boundaries for '{country}'.")
    return FileResponse(boundary_path(country, level), media_type="application/geo+json", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/solve", response_model=SolveResponse)
def solve(request: SolveRequest):
    """
    The main endpoint. Takes a depot + list of pickup stops (with
    demands) and returns the optimal route: which stops to visit, in
    what order, maximizing cartons picked (targeting the fill ratio)
    while minimizing distance/fuel cost.

    Validation: the first stop in the list MUST be the depot. We
    enforce demand=0 for it (a depot doesn't have cartons to pick up).
    """
    def stop_demand(stop: Stop) -> int:
        size_total = stop.small_boxes + stop.mid_boxes + stop.large_boxes
        return size_total if size_total > 0 or stop.demand is None else stop.demand

    excluded_regions = set(request.excluded_regions)
    excluded_districts = set(request.excluded_districts)

    def in_excluded_area(stop: Stop) -> bool:
        return stop.region in excluded_regions or stop.district in excluded_districts

    stops = [stop for stop in request.stops if not in_excluded_area(stop)]
    region_excluded_stops = [
        {
            "name": stop.name, "code": stop.source_location_id, "is_depot": stop.is_depot,
            "region": stop.region, "district": stop.district, "batteries": stop_demand(stop),
        }
        for stop in request.stops if in_excluded_area(stop)
    ]
    if region_excluded_stops and not any(stop.is_depot for stop in stops):
        raise HTTPException(status_code=400, detail="Every warehouse is inside an excluded region/district - keep at least one warehouse's area included.")
    if len(stops) < 2:
        raise HTTPException(status_code=400, detail="No pickup stations are left after excluding the selected regions/districts.")

    demands = [stop_demand(stop) for stop in stops]

    # Multiple stops flagged is_depot enable multi-depot routing (see
    # cvrp_solver.solve_fleet); with none flagged, fall back to the
    # original convention of stops[0] being the sole depot.
    depot_positions = [i for i, stop in enumerate(stops) if stop.is_depot]
    if not depot_positions:
        if demands[0] != 0:
            raise HTTPException(
                status_code=400,
                detail="The first stop must be the depot with demand=0 (or flag one or more stops as is_depot).",
            )
        depot_positions = [0]
    elif any(demands[i] != 0 for i in depot_positions):
        raise HTTPException(status_code=400, detail="A depot-flagged stop must have demand=0.")
    depot_position_set = set(depot_positions)

    routing_stops = []
    routing_demands = []
    pickup_delivery_pairs = []
    origin_index = []  # routing_stops[k] came from request.stops[origin_index[k]]
    routing_depot_indices = []  # positions within routing_stops that are depots
    for index, stop in enumerate(stops):
        stop_data = stop.model_dump()
        # The whole station's faulty count, kept on every capacity part so the
        # report's faulty % is per station, not per part.
        stop_data["station_faulty_batteries"] = demands[index]
        if index in depot_position_set:
            routing_depot_indices.append(len(routing_stops))
            routing_stops.append(stop_data)
            routing_demands.append(0)
            origin_index.append(index)
            continue
        if request.shipment_mode == "different":
            if stop.drop_lat is None or stop.drop_lon is None:
                raise HTTPException(status_code=400, detail=f"Set a drop point for {stop.name}.")
            pickup_index = len(routing_stops)
            routing_stops.append(stop_data)
            routing_demands.append(demands[index])
            origin_index.append(index)
            drop_index = len(routing_stops)
            routing_stops.append({**stop_data, "name": f"{stop.name} (drop)", "lat": stop.drop_lat, "lon": stop.drop_lon, "demand": -demands[index]})
            routing_demands.append(-demands[index])
            origin_index.append(index)
            pickup_delivery_pairs.append((pickup_index, drop_index))
            continue
        remaining = demands[index]
        sizes_left = {key: stop_data[key] for key in ("small_boxes", "mid_boxes", "large_boxes")}
        if sum(sizes_left.values()) == 0:
            sizes_left["mid_boxes"] = remaining  # legacy demand-only stop
        part_number = 1
        while remaining > 0:
            part_demand = min(remaining, request.vehicle_capacity)
            part_data = dict(stop_data)
            if demands[index] > request.vehicle_capacity:
                part_data["name"] = f"{stop.name} (part {part_number})"
            part_data["demand"] = part_demand
            # Each part carries only its own share of the size breakdown, so
            # per-size totals across parts still add up to the real stop.
            to_assign = part_demand
            for key in ("small_boxes", "mid_boxes", "large_boxes"):
                taken = min(sizes_left[key], to_assign)
                part_data[key] = taken
                sizes_left[key] -= taken
                to_assign -= taken
            routing_stops.append(part_data)
            routing_demands.append(part_demand)
            origin_index.append(index)
            remaining -= part_demand
            part_number += 1

    stops_as_dicts = routing_stops
    demands = routing_demands
    routing_algorithm = request.routing_algorithm

    def get_osrm_matrix():
        return osrm_cost_and_time_matrices(stops_as_dicts, road_cost_per_km=request.road_cost_per_km)

    if request.routing_algorithm == "tomtom":
        try:
            # OSRM/TomTom supply real road distance and real route duration.
            # These are used both by OR-Tools and by the daily driving-hours
            # constraint.
            cost_matrix, travel_time_matrix = tomtom_cost_and_time_matrices(
                stops_as_dicts, road_cost_per_km=request.road_cost_per_km
            )
            routing_algorithm = "tomtom-live-traffic"
        except Exception as tomtom_error:
            # TomTom can fail for reasons unrelated to the request itself
            # (exhausted API credits, a key issue, an outage) — falling back
            # to OSRM keeps /solve usable instead of hard-failing the whole
            # request over a third-party billing/availability problem.
            try:
                cost_matrix, travel_time_matrix = get_osrm_matrix()
                routing_algorithm = f"osrm-real-road (tomtom unavailable: {tomtom_error})"
            except Exception as osrm_error:
                raise HTTPException(status_code=503, detail=f"Real road service unavailable: {osrm_error}")
    else:
        try:
            cost_matrix, travel_time_matrix = get_osrm_matrix()
            routing_algorithm = "osrm-real-road"
        except Exception as osrm_error:
            raise HTTPException(status_code=503, detail=f"Real road service unavailable: {osrm_error}")

    toll_tax_total = sum(request.toll_taxes)

    try:
        result = solve_fleet(
            cost_matrix, demands, request.vehicle_capacity,
            max_vehicles=request.max_trucks or max(1, sum(demands) // request.vehicle_capacity + len(stops)),
            depot_index=routing_depot_indices,
            prefer_more_stops=request.prefer_more_stops,
            travel_time_matrix=travel_time_matrix,
            # A truck's total driving budget over its whole trip, not just one
            # day — max_trip_days > 1 lets it range further before it must
            # return to depot, resting overnight along the way rather than
            # being forced back the same day (see cvrp_solver.solve_fleet's
            # "DrivingTime" dimension: it's a cumulative cap over the whole
            # route, so a bigger budget naturally allows a multi-day trip).
            max_driving_seconds=int(request.max_driving_hours * 3600 * request.max_trip_days),
            pickup_delivery_pairs=pickup_delivery_pairs,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    route_coordinates = [
        {"lat": stops_as_dicts[i]["lat"], "lon": stops_as_dicts[i]["lon"]}
        for route_data in result["routes"] for i in route_data["stops"]
    ]

    def route_matrix_cost_of(route_stops):
        return sum(
            cost_matrix[start][end]
            for start, end in zip(route_stops, route_stops[1:])
        )

    depot_index_set = set(routing_depot_indices)

    def stop_details_of(route_stops):
        details = []
        cumulative_km = cumulative_fuel = cumulative_seconds = 0.0
        previous = None
        for i in route_stops:
            leg_cost = cost_matrix[previous][i] if previous is not None else 0
            leg_km = distance_km_from_matrix_cost(leg_cost, request.road_cost_per_km)
            leg_fuel = fuel_cost_from_matrix_cost(leg_cost, request.road_cost_per_km)
            leg_seconds = travel_time_matrix[previous][i] if previous is not None else 0
            cumulative_km += leg_km
            cumulative_fuel += leg_fuel
            cumulative_seconds += leg_seconds
            stop_data = stops_as_dicts[i]
            details.append({
                "name": stop_data["name"],
                "code": stop_data.get("source_location_id"),
                "country": stop_data.get("country"),
                "region": stop_data.get("region"),
                "district": stop_data.get("district"),
                "is_depot": i in depot_index_set,
                "small_boxes": stop_data.get("small_boxes", 0),
                "mid_boxes": stop_data.get("mid_boxes", 0),
                "large_boxes": stop_data.get("large_boxes", 0),
                "batteries": demands[i],
                "station_faulty_batteries": stop_data.get("station_faulty_batteries"),
                "working_batteries": stop_data.get("working_batteries"),
                "leg_km": round(leg_km, 2),
                "cumulative_km": round(cumulative_km, 2),
                "leg_fuel_cost": round(leg_fuel, 2),
                "cumulative_fuel_cost": round(cumulative_fuel, 2),
                "leg_hours": round(leg_seconds / 3600, 2),
                "cumulative_hours": round(cumulative_seconds / 3600, 2),
            })
            previous = i
        return details

    truck_routes = []
    for index, route_data in enumerate(result["routes"]):
        route_stops = route_data["stops"]
        truck_matrix_cost = route_matrix_cost_of(route_stops)
        truck_distance_km = distance_km_from_matrix_cost(truck_matrix_cost, request.road_cost_per_km)
        truck_fuel_cost = fuel_cost_from_matrix_cost(truck_matrix_cost, request.road_cost_per_km)
        truck_routes.append({
            "truck": index + 1,
            "visit_order": route_stops,
            "visit_order_names": [stops_as_dicts[i]["name"] for i in route_stops],
            "visit_order_codes": [stops_as_dicts[i].get("source_location_id") for i in route_stops],
            "boxes": [demands[i] for i in route_stops],
            "total_batteries_picked": sum(demands[i] for i in route_stops if demands[i] > 0),
            "distance_km": round(truck_distance_km, 1),
            "fuel_cost": round(truck_fuel_cost, 2),
            "driving_hours": round(route_data["driving_seconds"] / 3600, 2),
            "estimated_days": max(1, int(-(-route_data["driving_seconds"] // (request.max_driving_hours * 3600)))),
            "coordinates": [{"lat": stops_as_dicts[i]["lat"], "lon": stops_as_dicts[i]["lon"]} for i in route_stops],
            "stop_details": stop_details_of(route_stops),
        })

    route_matrix_cost = sum(route_matrix_cost_of(route_data["stops"]) for route_data in result["routes"])
    fuel_cost = fuel_cost_from_matrix_cost(route_matrix_cost, request.road_cost_per_km)
    total_operating_cost = fuel_cost + toll_tax_total
    visit_order = [node for route_data in result["routes"] for node in route_data["stops"]]

    dropped_expanded = set(result["dropped_stops"])
    # A stop can be split into capacity "parts" or a pickup/drop pair; if any
    # piece of an original stop got dropped, report that original stop as
    # dropped rather than silently under-counting it.
    dropped_original_indices = sorted({origin_index[i] for i in dropped_expanded})
    total_cartons_picked = sum(
        demands[index] for index in range(len(demands))
        if index not in dropped_expanded and demands[index] > 0
    )
    num_vehicles = result["num_vehicles"]
    fill_ratio = round(total_cartons_picked / (request.vehicle_capacity * num_vehicles), 3) if num_vehicles else 0.0

    return SolveResponse(
        visit_order=visit_order,
        visit_order_names=[stops_as_dicts[i]["name"] for i in visit_order],
        dropped_stops=dropped_original_indices,
        dropped_stop_names=[stops[i].name for i in dropped_original_indices],
        total_cartons_picked=total_cartons_picked,
        fill_ratio=fill_ratio,
        meets_fill_target=fill_ratio >= request.target_fill_ratio,
        num_stops_served=(len(stops) - len(depot_positions)) - len(dropped_original_indices),
        total_cost=round(total_operating_cost, 2),
        fuel_cost=round(fuel_cost, 2),
        toll_tax_total=toll_tax_total,
        total_operating_cost=round(total_operating_cost, 2),
        routing_algorithm=routing_algorithm,
        trucks_needed=result["num_vehicles"],
        truck_routes=truck_routes,
        route_coordinates=route_coordinates,
        dropped_stop_details=[
            {
                "name": stops[i].name,
                "code": stops[i].source_location_id,
                "country": stops[i].country,
                "region": stops[i].region,
                "district": stops[i].district,
                "small_boxes": stops[i].small_boxes,
                "mid_boxes": stops[i].mid_boxes,
                "large_boxes": stops[i].large_boxes,
                "batteries": stop_demand(stops[i]),
            }
            for i in dropped_original_indices
        ],
        region_excluded_stops=region_excluded_stops,
    )


class ReportRequest(BaseModel):
    result: dict = Field(..., description="The /solve response to build the report from")
    country: Optional[str] = None
    dataset: Optional[str] = None
    settings: dict = Field(default_factory=dict, description="Solve settings shown on the Overall Report sheet")


@app.post("/report/excel")
def route_report_excel(request: ReportRequest):
    """Builds the stop/trip/truck/overall route report workbook for a solve result."""
    from report_excel import build_route_report

    try:
        content = build_route_report(request.result, request.country, request.dataset, request.settings)
    except (KeyError, TypeError, IndexError) as e:
        raise HTTPException(status_code=400, detail=f"Result is missing data needed for the report ({e}). Solve the route again and retry.")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="route-report.xlsx"'},
    )
