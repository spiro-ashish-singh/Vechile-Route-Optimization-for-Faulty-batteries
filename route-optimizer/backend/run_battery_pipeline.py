"""
run_battery_pipeline.py

End-to-end test run for the "collect faulty batteries from swap
stations" use case: loads pending pickups per country from the CSV
export (battery_pickups.py), loads every REAL candidate drop-off
warehouse for that country (multi-depot — see cvrp_solver.solve_fleet),
builds a cost matrix, and runs the multi-vehicle solver to see how many
trucks each country needs and what each truck's route looks like.
"""

from battery_pickups import (
    stations_by_country_from_csv,
    load_warehouses,
    BATTERY_CSV_PATH,
    FUEL_PRICE_USD_PER_LITER,
    FUEL_CONSUMPTION_L_PER_KM,
)
from stops_and_cost import osrm_cost_and_time_matrices, fuel_cost_from_matrix_cost
from cvrp_solver import solve_fleet

TRUCK_CAPACITY = 300  # placeholder batteries-per-truck capacity
MAX_DRIVING_HOURS = 8
MAX_TRIP_DAYS = 3  # a truck may range up to 3 days before returning to depot


def run_country(country, stations):
    warehouses = load_warehouses(country)
    stops = warehouses + stations
    depot_indices = list(range(len(warehouses)))
    demands = [s["demand"] for s in stops]

    road_cost_per_km = FUEL_PRICE_USD_PER_LITER[country] * FUEL_CONSUMPTION_L_PER_KM
    # Real road distance/duration (OSRM), not the straight-line/35kph
    # estimate — that estimate ran ~2x slower than real roads for these
    # long-distance trips, wrongly pushing several stations over the
    # driving-hours budget that they actually fit within.
    cost_matrix, travel_time_matrix = osrm_cost_and_time_matrices(stops, road_cost_per_km=road_cost_per_km)

    print(f"\n=== {country}: {len(warehouses)} warehouses, {len(stations)} stations, {sum(demands)} batteries "
          f"(fuel: ${FUEL_PRICE_USD_PER_LITER[country]:.2f}/L -> ${road_cost_per_km:.3f}/km) ===")
    try:
        result = solve_fleet(
            cost_matrix, demands, TRUCK_CAPACITY,
            max_vehicles=max(1, sum(demands) // TRUCK_CAPACITY + len(stops)),
            depot_index=depot_indices,
            travel_time_matrix=travel_time_matrix,
            max_driving_seconds=int(MAX_DRIVING_HOURS * 3600 * MAX_TRIP_DAYS),
        )
    except RuntimeError as e:
        print(f"  FAILED: {e}")
        return

    print(f"  Trucks needed: {result['num_vehicles']}")
    total_fuel_cost = 0.0
    for i, route in enumerate(result["routes"], start=1):
        names = [stops[n]["name"] for n in route["stops"]]
        picked = sum(demands[n] for n in route["stops"])
        hours = round(route["driving_seconds"] / 3600, 2)
        matrix_cost = sum(
            cost_matrix[a][b] for a, b in zip(route["stops"], route["stops"][1:])
        )
        fuel_cost = fuel_cost_from_matrix_cost(matrix_cost, road_cost_per_km)
        total_fuel_cost += fuel_cost
        days = max(1, -(-hours // MAX_DRIVING_HOURS))
        print(f"  Truck {i}: {picked} batteries, {hours}h driving (~{int(days)} day trip), ${fuel_cost:.2f} fuel")
        print(f"    {' -> '.join(names)}")
    print(f"  Total fuel cost: ${total_fuel_cost:.2f}")
    if result["dropped_stops"]:
        dropped_names = [stops[n]["name"] for n in result["dropped_stops"]]
        print(f"  Dropped ({len(dropped_names)}): {', '.join(dropped_names)}")


if __name__ == "__main__":
    grouped, skipped = stations_by_country_from_csv(BATTERY_CSV_PATH)
    if skipped:
        print(f"Skipped (no coordinates): {', '.join(str(s) for s in skipped)}")
    for country, stations in grouped.items():
        run_country(country, stations)
