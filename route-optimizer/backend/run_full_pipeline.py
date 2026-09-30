"""
run_full_pipeline.py

This ties everything together end-to-end:
  1. Load pickup stops (real Nairobi locations + carton demands)
  2. Build a cost matrix (distance + fuel/road cost between every pair)
  3. Feed it to the CVRP solver (which stops to visit, in what order,
     to maximize cartons picked up within the 500-capacity limit)
  4. Print a human-readable shipment plan

Run this file directly to see the whole thing work together:
    python3 run_full_pipeline.py
"""

from stops_and_cost import nairobi_example_stops, haversine_cost_matrix
from cvrp_solver import solve_cvrp


def run(vehicle_capacity=500, target_fill_ratio=0.90, prefer_more_stops=True):
    stops = nairobi_example_stops()
    demands = [s["demand"] for s in stops]
    total_demand = sum(demands)

    cost_matrix = haversine_cost_matrix(stops)

    result = solve_cvrp(
        cost_matrix, demands, vehicle_capacity, depot_index=0,
        target_fill_ratio=target_fill_ratio, prefer_more_stops=prefer_more_stops,
    )

    # --- Print a readable shipment plan ---
    print("=" * 60)
    print("SHIPMENT PLAN")
    print("=" * 60)
    print(f"Truck capacity:        {vehicle_capacity} cartons")
    print(f"Total demand:          {total_demand} cartons across {len(stops)-1} stops")
    print(f"Cartons picked:        {result['total_cartons_picked']} "
          f"({100*result['total_cartons_picked']/total_demand:.0f}% of demand, "
          f"{result['fill_ratio']*100:.1f}% of truck capacity)")
    print(f"Meets fill target:     {result['meets_fill_target']} (target: {target_fill_ratio*100:.0f}%+)")
    print(f"Stops served:          {result['num_stops_served']}")
    print(f"Stops dropped:         {len(result['dropped_stops'])}")
    print(f"Total route cost:      {result['total_cost']:.0f}")
    print()

    print("ROUTE (in visiting order):")
    for step, node_idx in enumerate(result["visit_order"]):
        stop = stops[node_idx]
        if node_idx == 0:
            print(f"  {step}. {stop['name']}  [START/END]")
        else:
            print(f"  {step}. {stop['name']}  (+{stop['demand']} cartons)")

    if result["dropped_stops"]:
        print("\nDROPPED (couldn't fit in truck capacity):")
        for node_idx in result["dropped_stops"]:
            stop = stops[node_idx]
            print(f"  - {stop['name']}  ({stop['demand']} cartons left behind)")

    return result


if __name__ == "__main__":
    run()
