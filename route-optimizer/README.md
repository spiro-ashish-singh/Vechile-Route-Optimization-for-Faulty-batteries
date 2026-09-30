# Truck Route + Capacity Optimizer

A single-truck pickup route optimizer: given a depot, multiple pickup
stops (each with a carton demand), and a truck capacity (default 500
cartons), this finds which stops to visit and in what order to:

1. Fill the truck as close to capacity as possible (default target: 90%+)
2. Cover as many pickup points as possible (configurable)
3. Minimize total route distance + fuel/road cost

It's built around a real optimization technique (Google OR-Tools'
vehicle routing solver — the same class of tool used in real logistics
systems), not a hand-rolled heuristic.

## What's in this project

```
backend/
  routing_engine.py    - Dijkstra / A* / learned-cost shortest-path (from the earlier prototype)
  ml_cost_model.py      - trains a model to predict a "true" road cost beyond raw distance
  cvrp_solver.py         - THE CORE: OR-Tools solver for the capacity/multi-stop problem
  stops_and_cost.py       - real African-region example stops (Nairobi) + cost matrix builder
  run_full_pipeline.py     - command-line demo: runs the whole thing, prints a shipment plan
  app.py                    - FastAPI web server exposing /solve as an API
frontend/
  index.html                 - Leaflet.js map UI: click to add stops, solve, see the route drawn
```

## How to run it

### 1. Install dependencies (one-time)

```bash
cd backend
pip install fastapi uvicorn osmnx networkx ortools scikit-learn numpy
```

### 2. Try the command-line version first (no browser needed)

This is the fastest way to confirm everything works before touching
the web layer:

```bash
cd backend
python3 run_full_pipeline.py
```

You should see a printed shipment plan: which stops were visited, how
many cartons were picked up, and which stops were dropped.

You can also run the individual pieces to see how each part works on
its own:

```bash
python3 routing_engine.py     # tests Dijkstra vs A* on a small synthetic road grid
python3 ml_cost_model.py      # trains the ML cost model, shows feature importance
python3 cvrp_solver.py        # tests the capacity solver with a hand-made example
python3 stops_and_cost.py     # shows the real Nairobi stop data + cost matrix
```

### 3. Start the backend server

```bash
cd backend
uvicorn app:app --reload --port 8000
```

Leave this running. Open http://localhost:8000/docs in a browser —
this is FastAPI's auto-generated interactive API tester, where you can
try `/solve` directly without the map UI at all, which is a good way
to sanity-check the API before using the frontend.

### 4. Open the map

Just open `frontend/index.html` directly in your browser (double-click
it, or right-click → Open With → your browser). No build step needed.

- Click **"Load example: Nairobi"** to load the ready-made scenario
- Or click on the map yourself: **first click = depot**, every click
  after that adds a pickup stop (edit its carton demand in the left panel)
- Adjust truck capacity, fill target %, and fuel cost per km as you like
- Click **"Solve route"** — the optimal route draws on the map, served
  stops turn green, dropped stops turn red

If the API URL field at the top doesn't match where your backend is
running, update it (default assumes `http://localhost:8000`).

## Using real road data instead of straight-line distance

Right now, distances are computed as straight-line ("as the crow
flies") distance with a 1.35x detour-factor adjustment — this needs no
internet access and is good enough to prototype with.

To use REAL road network distances (actual driving distance through
real streets), swap this line in `app.py`:

```python
from stops_and_cost import haversine_cost_matrix
```

for:

```python
from stops_and_cost import road_network_cost_matrix
```

and change the corresponding function call in the `/solve` endpoint
to `road_network_cost_matrix(stops_as_dicts, place_name="Nairobi, Kenya", road_cost_per_km=...)`.

This needs internet access to OpenStreetMap servers (via the `osmnx`
library) and will be noticeably slower the first time (downloading the
road network for a whole city), so it's meant to run on your own
machine, not in a sandboxed environment.

## Extending this further (ideas for your write-up / next steps)

- **Swap in the ML cost model from `ml_cost_model.py`**: instead of a
  flat fuel-cost-per-km, use the trained model's road-segment cost
  (accounts for accident risk, congestion) as part of the CVRP distance
  matrix — combines both halves of the project into one system.
- **Multiple trucks**: `cvrp_solver.py`'s `solve_cvrp()` currently
  assumes 1 vehicle. OR-Tools supports multi-vehicle natively — you'd
  change `RoutingIndexManager(num_stops, 1, depot_index)`'s `1` to the
  number of trucks, and add a capacity per truck.
- **Time windows**: many real pickups have "only available 9am-11am"
  constraints — OR-Tools supports this via `AddDimension` with time
  windows, a natural next feature.
- **Evaluate against a naive baseline**: compare your solver's route
  cost/fill-rate against a simple "visit stops in order of size"
  heuristic, to quantify how much the optimization actually helps —
  good evidence for a portfolio or paper.
