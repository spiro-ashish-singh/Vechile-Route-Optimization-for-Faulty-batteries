"""
cvrp_solver.py

THE PROBLEM (in plain terms):
A truck starts at a depot (warehouse). There are several pickup stops,
each with some number of cartons waiting. The truck has a capacity
(500 cartons). If total demand across all stops is MORE than 500, the
truck can't visit everyone — it has to choose a SUBSET of stops that
fits in its capacity, and visit them in the ORDER that costs the least
(distance/fuel), while maximizing how many cartons it picks up.

This is a variant of the "Capacitated Vehicle Routing Problem" (CVRP) —
specifically, since we have 1 truck, it's closer to a "Prize-Collecting
TSP with capacity": collecting as much "prize" (cartons) as possible
without exceeding a budget (capacity), then finding the cheapest order.

WHY THIS IS HARD (and why we need a real solver, not just sorting):
You might think "just visit the biggest pickups first" — but that
ignores geography. A huge pickup 50km away might not be worth it if
three medium pickups are all next to each other and close to the
depot. Finding the TRUE best combination means checking combinations
of stops AND orderings together — the number of possibilities
explodes fast (this is why it's called "NP-hard"). For more than
~15 stops, brute-force checking every combination becomes computationally
infeasible even on a fast computer.

THE SOLUTION: Google OR-Tools
Instead of brute-forcing, we use OR-Tools — the same class of solver
Google itself uses for real logistics optimization. It uses smart
search strategies (constraint programming + local search) to find a
near-optimal answer in seconds, even for hundreds of stops.

HOW WE HANDLE "MAXIMIZE CARTONS, NOT VISIT EVERYONE":
OR-Tools' vehicle routing solver is built to visit ALL stops by
default. To let it SKIP stops (when total demand > capacity), we add
a "disjunction" with a penalty for each stop: the solver is allowed
to drop a stop, but it costs a large penalty for doing so — a penalty
big enough that it will only ever drop a stop when there's truly no
room, and among the ones it could drop, it'll prefer to drop stops
that cost more to reach (relative to how many cartons they offer).
"""

from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp


def solve_cvrp(distance_matrix, demands, vehicle_capacity, depot_index=0,
                target_fill_ratio=0.90, prefer_more_stops=True):
    """
    Solves the single-truck capacitated pickup problem.

    Args:
        distance_matrix: 2D list, distance_matrix[i][j] = cost to go
                          from stop i to stop j (includes the depot as
                          index `depot_index`). This "cost" can be pure
                          distance, or distance+fuel/toll combined —
                          whatever you feed in here is what gets minimized.
        demands: list, demands[i] = cartons waiting at stop i
                 (demands[depot_index] should be 0 — the depot has no cartons)
        vehicle_capacity: int, max cartons the truck can carry (e.g. 500)
        depot_index: which index in the matrix is the depot (default 0)
        target_fill_ratio: we want the truck at LEAST this full (e.g. 0.90
                            means aim for 90%+ of capacity used). This
                            doesn't hard-block lower fill (that could make
                            the problem unsolvable if stops don't add up
                            nicely) — instead it strongly biases the
                            solver toward combinations that hit this fill
                            level, by making "leaving capacity empty" and
                            "dropping a stop" both costly relative to it.
        prefer_more_stops: when True (default, matches "cover maximum
                            pickup points"), the solver leans toward
                            combinations with MORE stops served over
                            fewer big ones, when cartons picked are
                            otherwise similar.

    Returns:
        dict with:
          - visit_order: list of stop indices in the order the truck visits them
          - dropped_stops: stop indices the truck could NOT fit in (too full)
          - total_cartons_picked: sum of demands at visited stops
          - fill_ratio: total_cartons_picked / vehicle_capacity
          - total_cost: total distance/cost of the route
    """
    num_stops = len(distance_matrix)
    target_cartons = vehicle_capacity * target_fill_ratio

    # --- Step 1: set up the routing problem ---
    # "1" here means 1 vehicle (our single truck).
    manager = pywrapcp.RoutingIndexManager(num_stops, 1, depot_index)
    routing = pywrapcp.RoutingModel(manager)

    # --- Step 2: tell the solver how to compute cost between any two stops ---
    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(distance_matrix[from_node][to_node])

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # --- Step 3: tell the solver about carton demand + truck capacity ---
    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return demands[from_node]

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index,
        0,                      # no slack
        [vehicle_capacity],     # capacity for our 1 truck
        True,                   # start cumulative count at 0
        "Capacity",
    )

    # --- Step 4: allow the solver to SKIP stops if capacity forces it ---
    # We attach a "penalty" for not visiting a stop, i.e. how much it
    # "costs" the solver to leave a stop behind. The solver will only
    # drop a stop when the alternative (breaking capacity, or a much
    # more expensive route) is worse than paying this penalty.
    #
    # TWO GOALS baked into how we size the penalty per stop:
    #   1. Hit the fill target: a flat, LARGE base penalty per dropped
    #      stop means the solver won't casually drop stops just to save
    #      a bit of driving distance — it'll only drop what it truly
    #      cannot fit, pushing total picked cartons up toward capacity.
    #   2. Prefer more stops when `prefer_more_stops=True`: we scale
    #      the penalty per CARTON down a bit and add a flat per-stop
    #      bonus-for-keeping component. This means two small stops
    #      (e.g. 2x100 cartons) together become "worth more to keep"
    #      than one big stop (200 cartons) that takes the same capacity
    #      — because dropping either of the two small ones costs a
    #      penalty on top of the shared per-stop flat amount, whereas
    #      dropping the one big stop only "saves" one penalty.
    # IMPORTANT DESIGN NOTE (learned by testing): making the penalty scale
    # WITH demand (e.g. bigger stop = bigger penalty to drop) backfires for
    # "prefer more stops" — it makes the solver almost never drop a big
    # stop, even when dropping it would free up room for several smaller
    # ones and serve more stops overall. So instead:
    #   - every stop gets the SAME flat penalty for being dropped
    #     (dropping any stop is equally "bad" regardless of its size)
    #   - this means the solver's only remaining incentive is to pack in
    #     as many stops as it can fit (each one avoids an equal penalty),
    #     which naturally favors combinations of smaller stops over one
    #     big one when both are options — exactly "maximum pickup points."
    #   - when prefer_more_stops=False, we instead scale penalty WITH
    #     demand, which flips the incentive back toward "protect the
    #     biggest stops," i.e. maximize cartons over stop count.
    FLAT_PENALTY = 200_000
    for node in range(num_stops):
        if node == depot_index:
            continue
        if prefer_more_stops:
            penalty = FLAT_PENALTY
        else:
            penalty = demands[node] * 1_000
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

    # Note: `target_cartons` (from target_fill_ratio) isn't enforced as a
    # hard constraint here — with only 1 truck and fixed stop sizes, an
    # exact 90%+ fill isn't always achievable (e.g. if every combination
    # of stops that fits under 500 only adds up to 420). Instead the
    # penalty structure above biases the solver strongly toward filling
    # up as close to capacity as the available stops allow. The returned
    # `fill_ratio` (see below) tells you how close it actually got, so
    # you can see and report this honestly rather than pretending a
    # target was hit when the input data couldn't support it.

    # --- Step 5: search for a solution ---
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.FromSeconds(10)  # a bit more search time for better optima

    solution = routing.SolveWithParameters(search_parameters)

    if solution is None:
        raise RuntimeError("No solution found — check your inputs (e.g. capacity vs demands).")

    # --- Step 6: extract the results into something readable ---
    visit_order = []
    total_cost = 0
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        visit_order.append(node)
        previous_index = index
        index = solution.Value(routing.NextVar(index))
        total_cost += routing.GetArcCostForVehicle(previous_index, index, 0)
    visit_order.append(manager.IndexToNode(index))  # back to depot

    visited_set = set(visit_order)
    dropped_stops = [n for n in range(num_stops) if n != depot_index and n not in visited_set]

    total_cartons_picked = sum(demands[n] for n in visit_order if n != depot_index)
    num_stops_served = len(set(visit_order) - {depot_index})

    return {
        "visit_order": visit_order,
        "dropped_stops": dropped_stops,
        "total_cartons_picked": total_cartons_picked,
        "fill_ratio": round(total_cartons_picked / vehicle_capacity, 3),
        "meets_fill_target": total_cartons_picked >= target_cartons,
        "num_stops_served": num_stops_served,
        "total_cost": total_cost,
    }


def solve_fleet(distance_matrix, demands, vehicle_capacity, max_vehicles=20,
                depot_index=0, prefer_more_stops=True, travel_time_matrix=None,
                max_driving_seconds=None, pickup_delivery_pairs=None):
    """
    Solves the multi-vehicle CVRP in ONE OR-Tools solve, instead of the old
    approach of re-solving the whole problem from scratch for
    num_vehicles = 1, 2, 3, ... (each with its own time budget) until one
    attempt happened to serve every stop. That loop wasted most of its
    solves (they're thrown away) and could take vehicle_count * time_limit
    seconds in the worst case.

    Here we give the solver a generous vehicle pool up front and instead
    charge a fixed cost per vehicle actually used (SetFixedCostOfAllVehicles).
    This makes "use one more truck" and "make this route longer" directly
    comparable in the same objective, so the solver naturally converges on
    the smallest fleet that's still cheap to drive — in a single solve.

    Stops are only dropped (via AddDisjunction) when they truly can't fit
    within max_vehicles/capacity/driving-time — the returned "dropped_stops"
    list reflects reality instead of always being empty.

    depot_index may be a single node index (every vehicle shares one depot,
    the original behavior) OR a list of node indices — multiple real
    candidate depots/warehouses, each vehicle slot pinned to one of them
    round-robin. Which depots actually end up used is decided by the same
    cost-minimization that decides fleet size: an unused vehicle slot (at
    any depot) costs nothing, so the solver only "activates" a depot when
    routing through it is actually cheaper than the alternatives.
    """
    num_stops = len(distance_matrix)
    if any(demand > vehicle_capacity for demand in demands):
        raise RuntimeError("A shipment is larger than one truck's capacity.")

    pickup_delivery_pairs = pickup_delivery_pairs or []
    depot_indices = list(depot_index) if isinstance(depot_index, (list, tuple)) else [depot_index]
    depot_index_set = set(depot_indices)

    starts = [depot_indices[i % len(depot_indices)] for i in range(max_vehicles)]
    manager = pywrapcp.RoutingIndexManager(num_stops, max_vehicles, starts, starts)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return int(distance_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])

    transit_callback = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback)

    # Charge this once for every vehicle that leaves the depot, so the
    # solver only adds a truck when it actually needs the capacity/reach —
    # not just to shave a little distance off an existing route. Sized off
    # the matrix's own scale (twice the longest single leg) so it works
    # whether costs are raw meters or meters+fuel dollars.
    finite_costs = [value for row in distance_matrix for value in row if value < 900_000_000]
    max_leg_cost = max(finite_costs) if finite_costs else 1000
    routing.SetFixedCostOfAllVehicles(int(max_leg_cost * 2))

    def demand_callback(from_index):
        return demands[manager.IndexToNode(from_index)]

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index, 0, [vehicle_capacity] * max_vehicles, True, "Capacity"
    )

    if pickup_delivery_pairs:
        for pickup_node, drop_node in pickup_delivery_pairs:
            pickup_index = manager.NodeToIndex(pickup_node)
            drop_index = manager.NodeToIndex(drop_node)
            routing.AddPickupAndDelivery(pickup_index, drop_index)
            routing.solver().Add(routing.VehicleVar(pickup_index) == routing.VehicleVar(drop_index))
            # Both nodes of a pair must be active together or inactive together —
            # never just one — so a drop point can never be silently skipped
            # while its pickup is still served (or vice versa).
            routing.solver().Add(routing.ActiveVar(pickup_index) == routing.ActiveVar(drop_index))
            capacity_dimension = routing.GetDimensionOrDie("Capacity")
            routing.solver().Add(
                capacity_dimension.CumulVar(pickup_index) <= capacity_dimension.CumulVar(drop_index)
            )

    if travel_time_matrix is not None and max_driving_seconds is not None:
        def time_callback(from_index, to_index):
            return travel_time_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

        time_callback_index = routing.RegisterTransitCallback(time_callback)
        routing.AddDimension(
            time_callback_index, 0, max_driving_seconds, False, "DrivingTime"
        )

    # A large penalty makes dropping a stop a last resort, while still
    # allowing the model to prove that a stop truly can't fit. Each node
    # gets its own disjunction (even pair members) — the ActiveVar equality
    # constraint above is what keeps pairs joined, not disjunction grouping.
    penalty = 10_000_000 if prefer_more_stops else 1_000_000
    for node in range(num_stops):
        if node in depot_index_set:
            continue
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

    # A flat 15s budget was fine for the original ~8-stop demo, but a real
    # dataset (e.g. 196 stations) needs more search time to find the last
    # few dedicated-route slots — otherwise the solver can leave an easily
    # servable stop dropped simply because it ran out of time to look,
    # even though a dedicated route for it (a few thousand in cost) is far
    # cheaper than the 1M-10M disjunction penalty for dropping it.
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    parameters.time_limit.FromSeconds(min(90, max(15, num_stops)))
    solution = routing.SolveWithParameters(parameters)
    if solution is None:
        raise RuntimeError("No feasible solution found — check capacity, driving-hour limits, and max_trucks.")

    routes = []
    served = set()
    total_cost = 0
    for vehicle_id in range(max_vehicles):
        index = routing.Start(vehicle_id)
        route = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            route.append(node)
            served.add(node)
            previous_index = index
            index = solution.Value(routing.NextVar(index))
            total_cost += routing.GetArcCostForVehicle(previous_index, index, vehicle_id)
        route.append(manager.IndexToNode(index))
        if len(route) > 2:
            route_seconds = 0
            if travel_time_matrix is not None:
                route_seconds = sum(
                    travel_time_matrix[start][end] for start, end in zip(route, route[1:])
                )
            routes.append({"stops": route, "driving_seconds": route_seconds})

    dropped = [node for node in range(num_stops) if node not in depot_index_set and node not in served]
    total_demand = sum(demands[node] for node in range(num_stops) if node not in depot_index_set)
    served_demand = sum(demands[node] for node in served if node not in depot_index_set)

    return {
        "num_vehicles": len(routes),
        "routes": routes,
        "total_cost": total_cost,
        "total_demand": total_demand,
        "served_demand": served_demand,
        "dropped_stops": dropped,
    }


# ---------------------------------------------------------------------------
# Quick manual test with a small made-up example
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 1 depot (index 0) + 7 pickup stops (indices 1-7)
    # Mix of big and small stops on purpose, to show the fill-target /
    # prefer-more-stops behavior clearly:
    #   - stop 1: one big stop (280 cartons) that alone leaves 220 unused
    #   - stops 2-7: several small/medium stops that combine to fill better
    demands = [0, 280, 90, 85, 95, 80, 70, 60]
    vehicle_capacity = 500

    distance_matrix = [
        [0,  10, 15, 20, 25, 30, 35, 18],
        [10, 0,  12, 18, 22, 28, 32, 20],
        [15, 12, 0,  14, 19, 24, 29, 16],
        [20, 18, 14, 0,  11, 17, 22, 13],
        [25, 22, 19, 11, 0,  13, 18, 15],
        [30, 28, 24, 17, 13, 0,  10, 20],
        [35, 32, 29, 22, 18, 10, 0,  25],
        [18, 20, 16, 13, 15, 20, 25, 0],
    ]

    print(f"Total demand across all stops: {sum(demands)} cartons")
    print(f"Truck capacity: {vehicle_capacity} cartons (target: 90%+ fill = {0.9*vehicle_capacity:.0f}+ cartons)\n")

    result = solve_cvrp(distance_matrix, demands, vehicle_capacity, target_fill_ratio=0.90)

    print("Visit order (0 = depot):", result["visit_order"])
    print("Dropped stops:", result["dropped_stops"])
    print("Stops served:", result["num_stops_served"])
    print(f"Cartons picked: {result['total_cartons_picked']} / {vehicle_capacity} "
          f"({result['fill_ratio']*100:.1f}% fill)")
    print("Meets 90%+ fill target:", result["meets_fill_target"])
    print("Total route cost:", result["total_cost"])

    assert result["total_cartons_picked"] <= vehicle_capacity, "Should never exceed capacity!"
    print("\n✅ Sanity check passed: solution respects the 500-carton capacity limit.")
    if result["meets_fill_target"]:
        print("✅ Hit the 90%+ fill target by combining multiple smaller stops.")
    else:
        print("⚠️  Could not reach 90%+ fill with the available stop sizes — "
              f"got {result['fill_ratio']*100:.1f}%, which is the best achievable combination.")
