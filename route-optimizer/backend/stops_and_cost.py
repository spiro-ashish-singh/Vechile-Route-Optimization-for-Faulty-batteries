"""
stops_and_cost.py

Bridges the gap between:
  (a) real-world geography (a depot + pickup stops in an African city)
  (b) the CVRP solver (which just wants a "cost to go from i to j" matrix)

WHAT "COST" MEANS HERE (per your requirement: distance + road cost):
For each pair of stops, cost = distance_meters + road_cost_factor, where
road_cost_factor represents things like:
  - fuel cost (proportional to distance, but road TYPE changes fuel
    efficiency — a highway is more fuel-efficient per km than a
    congested city road with lots of stop-and-go)
  - toll-style cost (some routes might cross toll roads)

WHY WE BUILD A "COST MATRIX" AT ALL:
The CVRP solver (cvrp_solver.py) doesn't know about maps, roads, or
geography — it just wants a grid of numbers: "cost from stop i to
stop j". This file's job is to compute that grid correctly, so all
the geography/roads knowledge lives here, and cvrp_solver.py can stay
purely about the optimization logic. This separation is good practice:
each file has ONE job.

TWO MODES:
1. `haversine_cost_matrix()` — quick straight-line-distance based
   version. No internet needed, good for immediate testing (used below).
2. `road_network_cost_matrix()` — the REAL version using actual road
   network + real routing distances via OSMnx. This needs internet
   access to OpenStreetMap, so it's meant to be run on your own
   machine, not in this sandbox.
"""

import math
import json
import os
from urllib.parse import quote
from urllib.request import Request, urlopen


_ROAD_GRAPH_CACHE = {}
TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "")


# ---------------------------------------------------------------------------
# Realistic example: a depot + pickup stops around Nairobi, Kenya
# ---------------------------------------------------------------------------
def nairobi_example_stops():
    """
    A depot (warehouse, e.g. an industrial area) + several pickup
    points (e.g. suppliers/markets) around Nairobi, with a carton
    demand at each. Coordinates are real Nairobi-area locations.

    Feel free to swap in a different city/country — just change the
    lat/lon/demand values, everything else works unchanged.
    """
    return [
        {"name": "Depot (Industrial Area warehouse)", "lat": -1.3167, "lon": 36.8500, "demand": 0},
        {"name": "Gikomba Market pickup",              "lat": -1.2833, "lon": 36.8283, "demand": 180},
        {"name": "Kariokor Market pickup",              "lat": -1.2794, "lon": 36.8300, "demand": 140},
        {"name": "Westlands pickup",                    "lat": -1.2676, "lon": 36.8108, "demand": 120},
        {"name": "Eastleigh pickup",                    "lat": -1.2790, "lon": 36.8460, "demand": 160},
        {"name": "Kibera edge pickup",                  "lat": -1.3133, "lon": 36.7820, "demand": 90},
        {"name": "Karen pickup",                        "lat": -1.3197, "lon": 36.7076, "demand": 110},
        {"name": "Thika Road pickup",                   "lat": -1.2306, "lon": 36.8912, "demand": 150},
    ]


# ---------------------------------------------------------------------------
# Shared cost-matrix decoding: every cost matrix in this file encodes
# cost = distance_m + (distance_m / 1000) * road_cost_per_km, i.e.
# cost = distance_m * (1 + road_cost_per_km / 1000). Given a summed matrix
# cost for a route and the road_cost_per_km it was built with, both the
# pure distance and the fuel-dollar component are recoverable from that
# one number. Centralized here so app.py and run_battery_pipeline.py
# don't each keep their own copy of this formula to (mis)maintain.
# ---------------------------------------------------------------------------
def distance_km_from_matrix_cost(matrix_cost, road_cost_per_km):
    return matrix_cost / (1000 + road_cost_per_km)


def fuel_cost_from_matrix_cost(matrix_cost, road_cost_per_km):
    return distance_km_from_matrix_cost(matrix_cost, road_cost_per_km) * road_cost_per_km


# ---------------------------------------------------------------------------
# Mode 1: quick straight-line-distance cost matrix (no internet needed)
# ---------------------------------------------------------------------------
def _haversine_meters(lat1, lon1, lat2, lon2):
    """Straight-line ('as the crow flies') distance between two GPS points, in meters."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def haversine_cost_matrix(stops, road_cost_per_km=8.0, straight_line_road_factor=1.35):
    """
    Builds a cost matrix using straight-line distance as a stand-in for
    real road distance (real roads always wind more than a straight
    line, so we multiply by a "detour factor" of ~1.35 to approximate
    that — a common rule of thumb when real road data isn't available yet).

    cost = adjusted_distance_meters + (adjusted_distance_km * road_cost_per_km)

    road_cost_per_km represents fuel + wear-and-tear cost per km — a
    stand-in for the "distance + road cost" requirement. Tune this
    number to make fuel cost matter more or less relative to raw distance.
    """
    n = len(stops)
    matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            straight_line = _haversine_meters(
                stops[i]["lat"], stops[i]["lon"], stops[j]["lat"], stops[j]["lon"]
            )
            adjusted_distance_m = straight_line * straight_line_road_factor
            fuel_cost = (adjusted_distance_m / 1000) * road_cost_per_km
            matrix[i][j] = adjusted_distance_m + fuel_cost

    return matrix


def estimated_driving_time_matrix(stops, average_speed_kph=35.0, straight_line_road_factor=1.35):
    """Estimate driving seconds between stops for daily shift limits."""
    n = len(stops)
    matrix = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            distance_m = _haversine_meters(
                stops[i]["lat"], stops[i]["lon"], stops[j]["lat"], stops[j]["lon"]
            ) * straight_line_road_factor
            matrix[i][j] = int(distance_m / (average_speed_kph * 1000 / 3600))
    return matrix


# The public OSRM demo server's /table endpoint has a hard location limit —
# empirically confirmed: 100 locations succeeds, 105 fails with a 400. Above
# that, requests are chunked into <= 100x100 blocks (same approach as
# TomTom's matrix limit, see _TOMTOM_MATRIX_CHUNK) and stitched together.
_OSRM_MATRIX_CHUNK = 100


def _osrm_table_request(origin_coords, dest_coords):
    """One raw call to OSRM's /table endpoint for a small origin/destination block."""
    combined = origin_coords + dest_coords
    coordinates = ";".join(f"{lon},{lat}" for lon, lat in combined)
    sources = ";".join(str(i) for i in range(len(origin_coords)))
    destinations = ";".join(str(len(origin_coords) + i) for i in range(len(dest_coords)))
    url = (
        "https://router.project-osrm.org/table/v1/driving/"
        f"{quote(coordinates, safe=';,')}?annotations=duration,distance"
        f"&sources={sources}&destinations={destinations}"
    )
    with urlopen(url, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM table request failed: {data.get('message', data.get('code'))}")
    return data["distances"], data["durations"]


def osrm_cost_and_time_matrices(stops, road_cost_per_km=8.0):
    """
    Get real road distances and travel durations from the OSRM table API.

    Requests are chunked into <= 100x100 blocks (see _OSRM_MATRIX_CHUNK)
    and stitched back into one full n x n matrix, so this works for any
    number of stops instead of hard-failing past OSRM's per-request
    location limit.
    """
    n = len(stops)
    coords = [(stop["lon"], stop["lat"]) for stop in stops]
    distances = [[999_999_999] * n for _ in range(n)]
    durations = [[999_999_999] * n for _ in range(n)]

    index_chunks = [
        list(range(start, min(start + _OSRM_MATRIX_CHUNK, n)))
        for start in range(0, n, _OSRM_MATRIX_CHUNK)
    ]

    for origin_indices in index_chunks:
        for dest_indices in index_chunks:
            sub_distances, sub_durations = _osrm_table_request(
                [coords[i] for i in origin_indices],
                [coords[j] for j in dest_indices],
            )
            for a, global_origin in enumerate(origin_indices):
                for b, global_dest in enumerate(dest_indices):
                    distance = sub_distances[a][b]
                    duration = sub_durations[a][b]
                    if distance is not None:
                        distances[global_origin][global_dest] = distance
                        durations[global_origin][global_dest] = int(duration) if duration is not None else 999_999_999

    cost_matrix = [
        [distance + (distance / 1000) * road_cost_per_km for distance in row]
        for row in distances
    ]
    return cost_matrix, durations


def osrm_route_geometry(coordinates):
    """
    Traces the real road-following path through an ordered list of stops
    via OSRM's /route endpoint. coordinates: list of (lat, lon) tuples in
    visiting order. Returns a list of [lat, lon] points along the actual
    roads (not straight lines between stops).

    Called SERVER-SIDE deliberately — OSRM's public /route endpoint
    started rejecting direct browser requests with a CORS error (no
    Access-Control-Allow-Origin header), even though the equivalent
    server-to-server request works fine, since CORS is a browser-only
    restriction. Routing this through the backend sidesteps it entirely.
    """
    coordinate_string = ";".join(f"{lon},{lat}" for lat, lon in coordinates)
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{quote(coordinate_string, safe=';,')}?overview=full&geometries=geojson&steps=false"
    )
    with urlopen(url, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM route request failed: {data.get('message', data.get('code'))}")

    return [[lat, lon] for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


# TomTom's synchronous Matrix Routing API has a hard limit on how many
# origin x destination cells one request can contain — empirically
# confirmed: 14x14 (196 cells) succeeds, 15x15 (225 cells) fails with
# "The matrix size and parameters combination violates the API
# limitations." This isn't specific to live traffic; it's a flat
# request-size cap. Anything above ~14 stops (e.g. "different"
# pickup/drop mode, which doubles the stop count) would otherwise hit
# this ceiling and fail the whole /solve call outright.
_TOMTOM_MATRIX_CHUNK = 14


def _tomtom_matrix_request(origins, destinations):
    """One raw call to the TomTom matrix endpoint for a small origin/destination block."""
    if not TOMTOM_API_KEY:
        raise RuntimeError(
            "TOMTOM_API_KEY environment variable is not set. "
            "Set it before requesting TomTom-based routing (OSRM remains available without it)."
        )
    body = {
        "origins": origins,
        "destinations": destinations,
        "options": {"travelMode": "truck", "traffic": "live", "departAt": "now"},
    }
    url = f"https://api.tomtom.com/routing/matrix/2?key={TOMTOM_API_KEY}"
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    results = data if isinstance(data, list) else data.get("data", data.get("results", []))
    if not results:
        raise RuntimeError(f"TomTom matrix request returned no results: {data}")
    return results


def tomtom_cost_and_time_matrices(stops, road_cost_per_km=8.0):
    """
    Get traffic-aware road costs and durations from TomTom Matrix Routing.

    Requests are chunked into <= 14x14 blocks (see _TOMTOM_MATRIX_CHUNK)
    and stitched back into one full n x n matrix, so this works for any
    number of stops instead of hard-failing past TomTom's per-request
    size limit.
    """
    n = len(stops)
    locations = [
        {"point": {"latitude": stop["lat"], "longitude": stop["lon"]}}
        for stop in stops
    ]
    distances = [[999_999_999] * n for _ in range(n)]
    durations = [[999_999_999] * n for _ in range(n)]

    index_chunks = [
        list(range(start, min(start + _TOMTOM_MATRIX_CHUNK, n)))
        for start in range(0, n, _TOMTOM_MATRIX_CHUNK)
    ]

    for origin_indices in index_chunks:
        for dest_indices in index_chunks:
            results = _tomtom_matrix_request(
                [locations[i] for i in origin_indices],
                [locations[j] for j in dest_indices],
            )
            for item in results:
                local_origin = item.get("originIndex", item.get("origin", {}).get("index"))
                local_destination = item.get("destinationIndex", item.get("destination", {}).get("index"))
                if local_origin is None or local_destination is None:
                    continue
                summary = item.get("routeSummary", item.get("summary", {}))
                distance = summary.get("lengthInMeters")
                duration = summary.get("travelTimeInSeconds")
                if distance is not None and duration is not None:
                    global_origin = origin_indices[local_origin]
                    global_destination = dest_indices[local_destination]
                    distances[global_origin][global_destination] = distance
                    durations[global_origin][global_destination] = duration

    costs = [[distance + distance / 1000 * road_cost_per_km for distance in row] for row in distances]
    return costs, durations


# ---------------------------------------------------------------------------
# Mode 2: REAL road network cost matrix (run this on your own machine)
# ---------------------------------------------------------------------------
def road_network_cost_matrix(stops, place_name="Nairobi, Kenya", road_cost_per_km=8.0, algorithm="astar"):
    """
    Uses OSMnx to build a REAL road network for the given place, then
    computes REAL shortest-path road distances (not straight-line)
    between every pair of stops.

    This needs internet access to OpenStreetMap servers, so run it on
    your own machine (pip install osmnx first). It will not run inside
    this sandbox, which has restricted network access.

    Returns the same shape as haversine_cost_matrix(), so you can swap
    one for the other without changing any other code.
    """
    import osmnx as ox
    import networkx as nx

    if algorithm not in {"astar", "dijkstra"}:
        raise ValueError("algorithm must be 'astar' or 'dijkstra'")

    if place_name not in _ROAD_GRAPH_CACHE:
        print(f"Downloading road network for {place_name}... (this can take 30s-2min)")
        G = ox.graph_from_place(place_name, network_type="drive")
        G = ox.add_edge_speeds(G)
        G = ox.add_edge_travel_times(G)
        _ROAD_GRAPH_CACHE[place_name] = G
    G = _ROAD_GRAPH_CACHE[place_name]

    for _, _, _, data in G.edges(keys=True, data=True):
        distance_m = data.get("length", 0)
        data["route_cost"] = distance_m + (distance_m / 1000) * road_cost_per_km

    def heuristic(node, goal):
        return _haversine_meters(
            G.nodes[node]["y"], G.nodes[node]["x"],
            G.nodes[goal]["y"], G.nodes[goal]["x"],
        ) * (1 + road_cost_per_km / 1000)

    # Snap each stop's lat/lon to the nearest real road intersection (node)
    node_ids = []
    for stop in stops:
        nearest_node = ox.distance.nearest_nodes(G, stop["lon"], stop["lat"])
        node_ids.append(nearest_node)

    n = len(stops)
    matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            try:
                if algorithm == "astar":
                    route_cost = nx.astar_path_length(
                        G, node_ids[i], node_ids[j],
                        heuristic=heuristic, weight="route_cost"
                    )
                else:
                    route_cost = nx.shortest_path_length(
                        G, node_ids[i], node_ids[j], weight="route_cost"
                    )
            except nx.NetworkXNoPath:
                route_cost = 999_999  # effectively "unreachable"; solver will avoid it
            matrix[i][j] = route_cost

    return matrix


# ---------------------------------------------------------------------------
# Quick manual test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    stops = nairobi_example_stops()

    print(f"{len(stops)} locations (1 depot + {len(stops)-1} pickup stops):")
    for i, s in enumerate(stops):
        tag = "DEPOT" if i == 0 else f"{s['demand']} cartons"
        print(f"  [{i}] {s['name']:35s} ({tag})")

    print(f"\nTotal carton demand across all stops: {sum(s['demand'] for s in stops)}")

    matrix = haversine_cost_matrix(stops)
    print(f"\nCost matrix built ({len(matrix)}x{len(matrix)}). Example costs from depot:")
    for j in range(1, len(stops)):
        print(f"  Depot -> {stops[j]['name']:35s} cost = {matrix[0][j]:.0f}")
