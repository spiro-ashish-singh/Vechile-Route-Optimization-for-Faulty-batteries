"""
routing_engine.py

This is the "brain" of the project. It loads a road network graph and
lets you find a route between two points using different algorithms:

  - dijkstra   : classic shortest-path, weight = distance
  - astar      : same result as dijkstra but faster, uses a heuristic
  - learned    : A* but the edge "cost" comes from a trained ML model
                 instead of raw distance (this is YOUR contribution)

Everything is built on top of `networkx`, a graph library, and
`osmnx`, which turns real street maps into a networkx graph for us.

WHY THIS FILE IS STRUCTURED THIS WAY:
Keeping the algorithm logic completely separate from the web server
(app.py, which we'll write next) means you can test and understand
the routing logic on its own, without needing a browser at all.
That's good practice generally, not just for this project.
"""

import time
import math
import networkx as nx


# ---------------------------------------------------------------------------
# 1. LOADING THE ROAD NETWORK
# ---------------------------------------------------------------------------
def load_city_graph(place_name: str, network_type: str = "drive"):
    """
    Downloads a real road network for a place (e.g. "Chennai, India")
    and returns it as a networkx graph.

    Each NODE is an intersection (with lat/lon).
    Each EDGE is a road segment (with length, speed limit, road type, etc.)

    NOTE: this needs internet access to OpenStreetMap servers, so it
    will only work when you run it on your own machine, not in this
    sandbox. We test the algorithms below on a small synthetic graph
    instead, then swap in this real function once you run it locally.
    """
    import osmnx as ox
    G = ox.graph_from_place(place_name, network_type=network_type)
    # Add great-circle distances etc. as edge attributes (osmnx does this automatically)
    G = ox.add_edge_speeds(G)      # estimates speed limits where missing
    G = ox.add_edge_travel_times(G)  # computes travel_time = length / speed
    return G


def build_synthetic_graph():
    """
    A small fake road network so we can develop and test the algorithms
    RIGHT NOW without needing internet access to OpenStreetMap.

    Think of this as a tiny 4x4 grid of intersections, like a few city
    blocks. Each node has lat/lon (fake but realistic-looking), each
    edge has a length (meters) and a speed limit (km/h).
    """
    G = nx.MultiDiGraph()

    # Create a 4x4 grid of intersections (nodes), spaced ~200m apart
    grid_size = 4
    spacing = 0.002  # roughly 200m in lat/lon degrees
    base_lat, base_lon = 13.0827, 80.2707  # Chennai coordinates, just as an anchor

    node_id = 0
    grid = {}
    for row in range(grid_size):
        for col in range(grid_size):
            lat = base_lat + row * spacing
            lon = base_lon + col * spacing
            G.add_node(node_id, y=lat, x=lon)
            grid[(row, col)] = node_id
            node_id += 1

    # Connect neighbors horizontally and vertically (both directions = two-way streets)
    import random
    random.seed(42)  # reproducible "randomness" for consistent testing

    for row in range(grid_size):
        for col in range(grid_size):
            current = grid[(row, col)]
            # Connect to the right neighbor
            if col + 1 < grid_size:
                neighbor = grid[(row, col + 1)]
                _add_road(G, current, neighbor, random)
            # Connect to the neighbor below
            if row + 1 < grid_size:
                neighbor = grid[(row + 1, col)]
                _add_road(G, current, neighbor, random)

    return G


def _add_road(G, u, v, random):
    """
    Helper: adds a two-way road between nodes u and v with randomized
    but realistic attributes:
      - length: meters
      - speed_kph: speed limit
      - road_type: residential / secondary / primary (affects reliability)
      - accident_score: fake "how risky is this road" signal (0 = safe, 1 = risky)
      - congestion_variance: how unpredictable travel time is on this road
    """
    length = random.uniform(150, 300)  # meters
    road_type = random.choice(["residential", "secondary", "primary"])
    speed_kph = {"residential": 30, "secondary": 50, "primary": 70}[road_type]
    accident_score = random.uniform(0, 1)
    congestion_variance = random.uniform(0, 1)

    travel_time = length / (speed_kph * 1000 / 3600)  # seconds

    attrs = dict(
        length=length,
        speed_kph=speed_kph,
        road_type=road_type,
        accident_score=accident_score,
        congestion_variance=congestion_variance,
        travel_time=travel_time,
    )
    G.add_edge(u, v, **attrs)
    G.add_edge(v, u, **attrs)  # two-way street


# ---------------------------------------------------------------------------
# 2. HEURISTIC FOR A* (straight-line distance between two points)
# ---------------------------------------------------------------------------
def haversine_heuristic(G):
    """
    A* needs a "heuristic" function: a fast estimate of remaining distance
    to the goal, so it can prioritize exploring in the right direction
    instead of blindly expanding like Dijkstra does.

    We use haversine distance (straight-line distance on a sphere, i.e.
    "as the crow flies") between two lat/lon points. It's a valid
    heuristic because it never OVERESTIMATES the real road distance
    (roads are never shorter than a straight line) — that guarantee is
    what makes A* still find the optimal route, just faster.
    """
    def heuristic(u, v):
        y1, x1 = G.nodes[u]["y"], G.nodes[u]["x"]
        y2, x2 = G.nodes[v]["y"], G.nodes[v]["x"]
        R = 6371000  # Earth radius in meters
        phi1, phi2 = math.radians(y1), math.radians(y2)
        dphi = math.radians(y2 - y1)
        dlambda = math.radians(x2 - x1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.asin(math.sqrt(a))
    return heuristic


# ---------------------------------------------------------------------------
# 3. THE THREE ALGORITHMS
# ---------------------------------------------------------------------------
def route_dijkstra(G, start, end, weight="length"):
    """
    Classic shortest path. Explores outward in all directions equally
    (no sense of "which way is the destination"). Guaranteed optimal,
    but slower than A* on large graphs because it wastes time exploring
    away from the goal too.
    """
    t0 = time.perf_counter()
    path = nx.dijkstra_path(G, start, end, weight=weight)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return _summarize(G, path, elapsed_ms, algorithm="dijkstra")


def route_astar(G, start, end, weight="length"):
    """
    Same guaranteed-optimal result as Dijkstra, but faster: uses the
    haversine heuristic to explore mostly TOWARD the goal instead of
    in every direction equally.
    """
    heuristic = haversine_heuristic(G)
    t0 = time.perf_counter()
    path = nx.astar_path(G, start, end, heuristic=heuristic, weight=weight)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return _summarize(G, path, elapsed_ms, algorithm="astar")


def route_learned(G, start, end, cost_model):
    """
    This is YOUR contribution: instead of using raw distance as the
    edge weight, we ask a trained ML model "what does this road segment
    really cost to use?" given its features (length, road type,
    accident risk, congestion variance) — then run A* on THAT cost.

    cost_model: an object with a .predict_edge_cost(edge_attrs) -> float method
                (we'll build this in the next file, ml_cost_model.py)
    """
    # Temporarily attach a "learned_cost" attribute to every edge in the
    # graph by asking the model to score it. This only needs to happen
    # once per graph in a real app (cache it), but for clarity we do it
    # fresh here.
    for u, v, k, data in G.edges(keys=True, data=True):
        data["learned_cost"] = cost_model.predict_edge_cost(data)

    heuristic = haversine_heuristic(G)
    t0 = time.perf_counter()
    path = nx.astar_path(G, start, end, heuristic=heuristic, weight="learned_cost")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return _summarize(G, path, elapsed_ms, algorithm="learned")


# ---------------------------------------------------------------------------
# 4. SUMMARIZING A ROUTE (so the frontend can display + compare results)
# ---------------------------------------------------------------------------
def _summarize(G, path, elapsed_ms, algorithm):
    """
    Turns a raw path (list of node IDs) into something useful:
      - the actual lat/lon coordinates (to draw on the map)
      - total distance
      - total estimated travel time
      - total accident risk (sum, so you can SEE what the learned route optimizes for)
      - how long the algorithm took to compute (performance comparison)
    """
    coords = [{"lat": G.nodes[n]["y"], "lon": G.nodes[n]["x"]} for n in path]

    total_length = 0
    total_time = 0
    total_accident_score = 0

    for u, v in zip(path[:-1], path[1:]):
        # A MultiDiGraph can have several edges between the same two nodes;
        # take the first (shortest) one, same convention osmnx uses.
        edge_data = G.get_edge_data(u, v)[0]
        total_length += edge_data.get("length", 0)
        total_time += edge_data.get("travel_time", 0)
        total_accident_score += edge_data.get("accident_score", 0)

    return {
        "algorithm": algorithm,
        "path_node_ids": path,
        "coordinates": coords,
        "distance_meters": round(total_length, 1),
        "travel_time_seconds": round(total_time, 1),
        "total_accident_score": round(total_accident_score, 2),
        "compute_time_ms": round(elapsed_ms, 3),
    }


# ---------------------------------------------------------------------------
# Quick manual test — run this file directly to sanity-check everything works
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    G = build_synthetic_graph()
    nodes = list(G.nodes)
    start, end = nodes[0], nodes[-1]  # opposite corners of the grid

    print(f"Testing route from node {start} to node {end}\n")

    result_dijkstra = route_dijkstra(G, start, end)
    print("DIJKSTRA:", result_dijkstra)

    result_astar = route_astar(G, start, end)
    print("\nA*:      ", result_astar)

    assert result_dijkstra["path_node_ids"] == result_astar["path_node_ids"], \
        "Dijkstra and A* should find the SAME optimal path (just at different speeds)"
    print("\n✅ Sanity check passed: Dijkstra and A* agree on the optimal route.")
