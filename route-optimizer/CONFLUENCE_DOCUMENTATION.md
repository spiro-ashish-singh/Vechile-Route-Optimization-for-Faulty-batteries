# Truck Route Optimizer — Technical & Design Documentation

> Draft prepared for Confluence. Paste directly into a new page (headings,
> tables, and code blocks convert cleanly via Confluence's Markdown import,
> or Insert → Markup → Wiki Markup / paste-as-Markdown depending on your
> Confluence version).

---

## 1. Overview

### 1.1 What this is
A route-optimization system that plans daily multi-truck pickup routes —
originally a generic "collect cartons from markets" demo, now built out
into a real operational use case: **collecting faulty batteries from swap
stations across Rwanda, Uganda, and Kenya, and delivering them to real
regional warehouses**, with a live map UI for planning and visualizing
routes.

### 1.2 Business problem
- Multiple pickup points (swap stations) each have a number of faulty
  batteries waiting for collection.
- A limited fleet of trucks, each with a capacity limit, must visit as
  many pickup points as possible and bring the batteries back to a
  warehouse.
- Countries have **multiple real candidate warehouses**, not one central
  depot — the system must decide which warehouse each truck should use.
- Trucks have a daily driving-hour limit, but may need to range further
  than one day allows (multi-day trips) to reach distant regions.
- Real road distances and (optionally) live traffic must inform the cost
  of a route, not straight-line distance.

### 1.3 Core outcome
Given a country's data, the system answers: **how many trucks are
needed, which warehouse does each start/end from, what order do they
visit their stops in, how much does each route cost, and which stops (if
any) genuinely can't be served** — and shows it all on an interactive map.

---

## 2. Architecture

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│   Frontend (index.html)     │  HTTP  │   Backend (FastAPI, app.py)  │
│   Leaflet map + vanilla JS  │◄──────►│   /solve, /battery-stations, │
│   no build step, no framework│       │   /route-geometry, /example- │
└──────────────┬───────────────┘        │   stops                     │
               │                        └───────────┬──────────────────┘
               │ tile images                          │
               ▼                                      ▼
   ┌────────────────────┐               ┌─────────────────────────────┐
   │ TomTom / Esri tiles │               │ cvrp_solver.py (OR-Tools)   │
   │ (basemap + traffic) │               │ stops_and_cost.py (matrices)│
   └────────────────────┘               │ battery_pickups.py (data)   │
                                          └───────────┬──────────────────┘
                                                       │
                                     ┌─────────────────┼─────────────────┐
                                     ▼                 ▼                 ▼
                              TomTom Matrix       OSRM (table +     Local CSV/XLSX
                              Routing API         route APIs)       (pickups + real
                              (live traffic)      (free, no key)     warehouses)
```

**Design principle throughout:** each backend file has one job.
`cvrp_solver.py` only knows optimization math (no geography).
`stops_and_cost.py` only knows geography → cost-matrix conversion (no
optimization). `battery_pickups.py` only knows the business data source
(CSV/XLSX loading). `app.py` is purely the glue between HTTP and those
three.

---

## 3. Tech Stack

| Layer | Technology |
|---|---|
| Optimization engine | **Google OR-Tools** (`ortools.constraint_solver`) — constraint programming + guided local search |
| Backend framework | **FastAPI** + **Pydantic** (validation) + **Uvicorn** (ASGI server) |
| Real road data | **TomTom Matrix Routing API** (live traffic, primary) and **OSRM** (free, no key, automatic fallback) |
| Data loading | **pandas** (Excel/CSV), plain `csv` module |
| Frontend | Vanilla JavaScript + **Leaflet.js** (map rendering), no framework, no build step |
| Map tiles | **TomTom** (`basic/main` style) and **Esri** `World_Street_Map` (fallback, OSRM mode) |
| Map screenshot | **html2canvas** (client-side PNG export of the live map) |
| AWS integration (on hold) | `boto3` (Athena + S3), currently blocked on an AWS permissions/Lake-Formation issue on the client's account |

No database — everything is computed fresh per request from CSV/XLSX
source files and in-memory request data. No authentication layer (local
dev / prototype stage).

---

## 4. The Optimization Algorithm (deep dive)

### 4.1 Problem class
This is a **Capacitated Vehicle Routing Problem (CVRP)** with several
real-world extensions:
- **Multi-depot** — several valid start/end warehouses per country, not
  one fixed depot.
- **Prize-collecting / optional visits** — not every stop must be
  served; stops can be dropped if they truly don't fit, at a heavy
  penalty.
- **Pickup-and-delivery pairing** — for the "different pickup/drop
  point" shipment mode, a pickup and its corresponding drop must always
  travel together on the same vehicle.
- **Time-dimension (driving hours) constraint** — a cumulative driving
  budget per vehicle, extendable to multi-day trips.

This is NP-hard — brute-forcing all combinations of which stops to
serve, in what order, by which truck, from which depot, is computationally
infeasible beyond a handful of stops. OR-Tools' constraint solver +
metaheuristic search finds a near-optimal answer in seconds even for
hundreds of stops.

### 4.2 Why OR-Tools, and which strategy
- **First solution strategy:** `PATH_CHEAPEST_ARC` — builds an initial
  feasible route greedily by always extending with the cheapest next arc.
- **Local search metaheuristic:** `GUIDED_LOCAL_SEARCH` — the standard
  choice for VRPs; escapes local optima by penalizing repeatedly-used
  expensive arcs, standard in real logistics optimization (this is the
  same solver class Google itself uses operationally).
- **Search time budget:** scales with problem size —
  `min(90, max(15, num_stops))` seconds — because a flat 15s budget
  (fine for an 8-stop demo) was empirically proven **insufficient** for
  a 196-station real dataset: the solver would leave easily-servable
  stops dropped simply because it ran out of time to look, even though
  serving them was far cheaper than the drop penalty.

### 4.3 Fleet-size minimization (single solve, not a loop)
**Old design (replaced):** loop `num_vehicles = 1, 2, 3, ...`,
re-solving the *entire* problem from scratch each time until one attempt
happened to serve every stop. Wasteful — most solves are discarded, and
worst-case cost scales with vehicle count × time limit.

**Current design:** give the solver a generous vehicle pool up front,
and charge a **fixed cost per vehicle actually used**
(`SetFixedCostOfAllVehicles`, sized off the matrix's own scale — twice
the longest single leg in the cost matrix). This makes "add one more
truck" and "make an existing route longer" directly comparable in the
same objective function, so the solver naturally converges on the
smallest sufficient fleet **in a single solve**.

### 4.4 Multi-depot routing
```python
starts = [depot_indices[i % len(depot_indices)] for i in range(max_vehicles)]
manager = pywrapcp.RoutingIndexManager(num_stops, max_vehicles, starts, starts)
```
Each vehicle slot is pinned to one candidate depot (round-robin across
however many real warehouses exist for that country). Because an unused
vehicle slot costs nothing, and a used one costs the fixed vehicle fee,
**the solver itself decides which warehouses actually get used** — the
same cost-minimization mechanism that decides fleet size also decides
depot selection. No separate "assign trucks to warehouses" step is
needed.

This directly solved a real geography problem: with one central depot
per country, distant regions (e.g. Uganda's north, Kenya's coast)
required drop-penalty trade-offs that looked like a solver limitation
but were actually a *modeling* limitation — once real regional
warehouses were available as multi-depot candidates, the same solver
covered those regions cleanly (e.g. coastal Kenya pickups routing
through the Mombasa warehouse instead of Nairobi).

### 4.5 Optional stops (disjunctions) and honesty about drops
```python
routing.AddDisjunction([manager.NodeToIndex(node)], penalty)
```
Every non-depot node gets a disjunction: the solver *may* skip it, but
pays a penalty (10,000,000 by default, or 1,000,000 if
`prefer_more_stops=False`) for doing so — large enough that dropping is
only ever a last resort compared to adding a route detour or an extra
truck. `dropped_stops` in the response reflects real infeasibility
(capacity/time truly can't fit it), not a hardcoded/optimistic default.

### 4.6 Pickup/delivery pairing correctness
For "pickup and drop are different" mode, each pair must travel
together — never just one side:
```python
routing.AddPickupAndDelivery(pickup_index, drop_index)
routing.solver().Add(routing.VehicleVar(pickup_index) == routing.VehicleVar(drop_index))
routing.solver().Add(routing.ActiveVar(pickup_index) == routing.ActiveVar(drop_index))
```
The `ActiveVar` equality constraint is the textbook-correct way to
guarantee **both-or-neither** — verified by test: under a tight
driving-time budget forcing a drop, the pair drops together, never
independently.

### 4.7 Driving-time dimension and multi-day trips
```python
routing.AddDimension(time_callback_index, 0, max_driving_seconds, False, "DrivingTime")
```
A cumulative time budget per vehicle over its **whole route**, not
per-day. `max_driving_seconds = max_driving_hours * 3600 * max_trip_days`
— so a `max_trip_days > 1` value lets a truck range further before
having to return, modeling an implicit overnight rest without needing an
explicit multi-day scheduling model. This is what allows genuinely
distant stations to be served at all, rather than being permanently
unreachable under a same-day-only assumption.

### 4.8 Capacity dimension
Standard `AddDimensionWithVehicleCapacity` — each vehicle accumulates
demand along its route and cannot exceed `vehicle_capacity`. Any single
shipment larger than one truck's capacity raises an explicit error
rather than silently failing.

---

## 5. End-to-End Data Flow

### 5.1 Battery-pickup flow (the real use case)
```
1. Source data:
   - faulty_batteries.csv  → per-station pending battery counts (country,
     name, id, lat, lon, count)
   - warehouse/*.csv|xlsx  → real candidate warehouses per country
     (different column layouts per country, handled per-file)

2. battery_pickups.py:
   - faulty_battery_stations_from_csv(country) → pickup stops
   - load_warehouses(country) → depot candidates
     - filters test/placeholder rows (name contains "test"/"(old)")
     - filters swap-station IDs that leaked into the warehouse file
       (ID contains "-SS-", confirmed real case: RW-SS-0000273)
     - handles missing/NaN lat-lon defensively

3. GET /battery-stations?country=X (app.py):
   - returns depot_stops (is_depot=true) + pickup stops
   - also returns road_cost_per_km (real fuel price × placeholder
     consumption rate) so the frontend can pre-fill the correct cost

4. Frontend loads these into `stops[]`, renders markers
   (gold = depot, green = pickup), user clicks "Solve route"

5. POST /solve (app.py):
   a. Validates depot(s), expands "different" shipment pairs and
      capacity-exceeding splits into routing-safe entries
   b. Builds a cost matrix via TomTom (default) or OSRM (fallback or
      explicit choice) — chunked automatically past each provider's
      per-request size limit (see §6.3)
   c. Calls cvrp_solver.solve_fleet() with the multi-depot list,
      capacity, driving-time budget, and pickup/delivery pairs
   d. Formats the result: per-truck routes, distance, fuel cost,
      batteries picked, driving hours, estimated trip days, and an
      honestly-computed dropped-stops list

6. Frontend renders:
   - one color-coded polyline per truck (real road-following geometry
     via POST /route-geometry, itself proxying OSRM server-side)
   - a triangular "S" marker per truck's start/end warehouse (offset
     visually if multiple trucks share the same real warehouse, with a
     connector segment so the line still visibly reaches it)
   - sequential numbered markers (1, 2, 3, ...) for each stop in
     visiting order
   - a route-order text list per truck, with distance/fuel/batteries/
     hours summary
```

### 5.2 Generic demo flow (Nairobi example)
Same `/solve` endpoint, but stops come from a hardcoded example dataset
(`nairobi_example_stops()`) with a single depot at index 0 — exercises
the "legacy" single-depot path (no `is_depot` flags needed; `stops[0]`
is assumed to be the depot for backward compatibility).

---

## 6. Backend Reference

### 6.1 File-by-file
| File | Responsibility |
|---|---|
| `app.py` | FastAPI server — HTTP endpoints, request validation, response formatting. No optimization or geography logic of its own. |
| `cvrp_solver.py` | OR-Tools model construction and solving. Pure optimization — knows nothing about maps or CSV files. |
| `stops_and_cost.py` | Converts geography (lat/lon pairs) into cost/time matrices, via TomTom, OSRM, or straight-line haversine estimate. Also proxies OSRM's `/route` for real road-following polylines. |
| `battery_pickups.py` | Loads real business data — pending pickups and warehouse candidates — from CSV/XLSX files (or, eventually, Athena). |
| `connection.py` | AWS Athena/S3 client setup (currently unused — on hold, see §8). |
| `run_battery_pipeline.py` | Standalone CLI script: runs the full pipeline per country outside the web app, useful for quick testing/tuning without the browser. |
| `run_full_pipeline.py` | Older CLI demo using the single-truck `solve_cvrp()` and the Nairobi example data. |
| `frontend/index.html` | The entire UI — map, controls, results panel — single file, no build step. |

### 6.2 API Endpoints
| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Health check |
| `GET` | `/example-stops` | Returns the hardcoded Nairobi demo dataset |
| `GET` | `/battery-stations?country=X` | Returns real pickup stations + real warehouse candidates for a country, plus the real fuel-cost-per-km |
| `POST` | `/solve` | The main optimization call — see request/response shape below |
| `POST` | `/route-geometry` | Server-side OSRM proxy — traces real road geometry through an ordered coordinate list (avoids a browser-side CORS block on OSRM's `/route` endpoint) |

#### `/solve` — key request fields
| Field | Meaning |
|---|---|
| `stops` | List of `{name, lat, lon, small/mid/large_boxes, is_depot, drop_lat/lon}` |
| `vehicle_capacity` | Per-truck capacity |
| `routing_algorithm` | `"tomtom"` (default, live traffic) or `"osrm"` (free, no traffic) |
| `max_driving_hours`, `max_trip_days` | Daily driving cap × how many days a trip may span |
| `shipment_mode` | `"same"` (pickup=drop) or `"different"` (paired pickup/drop points) |
| `prefer_more_stops` | Biases the drop-penalty scaling toward serving more stops vs. maximizing total volume |
| `toll_taxes` | Flat toll amounts added to total cost |

#### `/solve` — key response fields
`trucks_needed`, `truck_routes[]` (per truck: `visit_order`,
`visit_order_names`, `distance_km`, `fuel_cost`,
`total_batteries_picked`, `driving_hours`, `estimated_days`,
`coordinates`), `dropped_stops`/`dropped_stop_names`, `fill_ratio`,
`meets_fill_target`, `total_operating_cost`, `routing_algorithm` (echoes
back which provider *actually* computed the route, including a fallback
explanation if TomTom failed).

### 6.3 Real-road API size limits (both discovered and fixed)
| Provider | Empirical limit | Fix |
|---|---|---|
| TomTom Matrix Routing | 196 cells (14×14) — 225 cells fails with `"matrix size... violates API limitations"` | Chunked into ≤14×14 blocks, stitched into one full matrix |
| OSRM `/table` | 100 locations — 105 fails with `400` | Chunked into ≤100×100 blocks, stitched the same way |

Both limits are **not traffic-related** — they're flat request-size
caps that applied even without live traffic enabled, confirmed by direct
testing against the raw APIs.

### 6.4 Cost-matrix encoding
Every cost matrix in the system encodes:
```
cost = distance_m + (distance_m / 1000) * road_cost_per_km
     = distance_m * (1 + road_cost_per_km / 1000)
```
Both the pure distance and the fuel-dollar component are recoverable
from a summed route cost — centralized as `distance_km_from_matrix_cost`
/ `fuel_cost_from_matrix_cost` in `stops_and_cost.py`, used consistently
by both `app.py` and `run_battery_pipeline.py` (previously duplicated
copies of this formula existed in both files and were unified to avoid
drift).

---

## 7. Frontend Features

- **Map**: Leaflet.js, TomTom streets basemap by default, Esri streets as
  an automatic fallback whenever OSRM (not TomTom) actually computed the
  route — so the map never shows TomTom branding for a result TomTom
  didn't produce.
- **Live traffic overlay**: opt-in via the layer control, automatically
  disabled/greyed out whenever OSRM is the active provider (traffic has
  no meaning without TomTom).
- **Stops list**: box-quantity inputs (small/mid/large — labeled
  "batteries" for this use case), per-stop remove, depot checkboxes to
  **manually include/exclude specific warehouses** from a solve without
  reloading data.
- **Numbered route markers**: a triangular "S" per truck start/end
  warehouse (SVG-based, not CSS `clip-path`, so it also renders correctly
  in the downloaded map image — see below), sequential numbers for each
  stop in visiting order. When multiple trucks share the same real
  warehouse, later markers fan out visually (golden-angle spiral) with a
  connector segment so the route line still visibly reaches each one.
- **Download map**: rasterizes the live map (tiles + routes + markers)
  to a PNG via `html2canvas`, with Leaflet's own UI chrome hidden during
  capture. Both basemap providers were confirmed to send
  `Access-Control-Allow-Origin: *`, avoiding the "tainted canvas" problem
  that usually blocks this kind of feature.
- **Results panel**: total batteries picked, trucks needed, fill %,
  fuel/toll/total cost breakdown, and a per-truck route list showing
  distance, fuel cost, batteries picked, and driving hours/estimated
  trip days in one line.
- **No manual stop entry**: map-click-to-add-a-stop was deliberately
  removed — every stop must come from real source data (the example
  dataset or the battery CSV/warehouse files), preventing accidental
  unsourced stops from polluting a real dataset.

---

## 8. Known Limitations & Placeholders

| Item | Status |
|---|---|
| Truck fuel consumption rate (12 L/100km) | Placeholder — real fuel *prices* per country are real, but the vehicle consumption figure is assumed |
| Truck capacity (300 batteries in test runs) | Placeholder |
| TomTom Matrix Routing credits | Currently exhausted on the account tied to the embedded key — `/solve` automatically falls back to OSRM when this happens |
| Athena/AWS integration | On hold — confirmed AWS credentials work, but the account lacks Glue/Lake-Formation visibility into the `landing_db` schema; CSV export is the working substitute |
| AWS & TomTom keys | Currently hardcoded in `credentials.py` / `stops_and_cost.py` — **should be rotated and moved to environment variables / secrets management** before any wider deployment |
| Hardcoded file paths | Overridable via environment variables (`BATTERY_CSV_PATH`, `RWANDA_WAREHOUSE_PATH`, etc.) but default to one developer's local machine |
| No authentication | Anyone who can reach the backend can call `/solve` — fine for local/prototype use, not for a shared deployment |
| No persistence | Every result is computed fresh per request; nothing is stored |

---

## 9. Notable Correctness Fixes (worth knowing for future maintenance)

- **Fleet-size loop → single solve**: old design re-solved from scratch
  per candidate fleet size; replaced with fixed-cost-per-vehicle in one
  solve (§4.3).
- **Pickup/delivery pairs could split**: fixed with an explicit
  `ActiveVar` equality constraint (§4.6), verified by test.
- **`dropped_stops` was always empty / `meets_fill_target` was
  hardcoded `True`**: now computed honestly from actual solver output.
- **Search time budget too short for real data size**: scaled with
  problem size instead of a flat constant tuned for an 8-stop demo.
- **Single-depot assumption**: replaced with genuine multi-depot
  support once real warehouse data became available — this is what
  actually fixed the "can't reach distant regions" problem, not a
  bigger time budget or more trucks.
- **A swap-station ID had leaked into a warehouse file**: filtered
  generically (any `-SS-` ID pattern), not just the one confirmed case.
- **Route-line/marker mismatch**: the visual "fan out" fix for
  overlapping same-warehouse markers initially moved only the marker,
  leaving the route line ending at the true (un-offset) coordinate —
  fixed by computing the offset once and extending the line with a
  connector segment to match.
- **`html2canvas` can't rasterize CSS `clip-path`**: the start/end
  triangle marker was rebuilt as inline SVG so the downloaded map image
  matches the live map exactly.

---

## 10. Suggested Roadmap

1. Replace placeholder truck capacity/fuel-consumption with real fleet
   specs.
2. Resolve TomTom credit / Athena permissions so both real-time-traffic
   routing and the live data source (instead of CSV export) are usable
   end-to-end.
3. Move all API keys/credentials to environment variables or a secrets
   manager; rotate the currently-exposed TomTom and AWS keys.
4. Replace the temporary Cloudflare quick-tunnel sharing mechanism with
   a proper always-on deployment (e.g. Render/Railway for the backend,
   Netlify/Vercel for the frontend) if this needs to be reachable
   without a developer's machine running.
5. Consider basic authentication/rate-limiting before sharing the app
   beyond a small trusted group, since `/solve` is currently open to
   anyone who can reach the URL.
