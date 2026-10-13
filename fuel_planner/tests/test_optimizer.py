import itertools
import math
import random

import numpy as np
import pytest

from fuel_planner.services.optimizer import (
    CandidateStation,
    InfeasibleRouteError,
    Vehicle,
    _greedy,
    _purchases,
    plan_fuel_stops,
)

TRUCK = Vehicle(range_miles=500, mpg=10)


def station(ref, mile, price):
    return CandidateStation(ref=ref, mile=mile, price=price)


def exact_min_cost(positions, prices, start_fuel, tank):
    """Exact optimum by dynamic programming over integer fuel levels (miles of range).

    With integer positions the optimal purchase amounts are integers (the constraint
    matrix is an interval matrix), so this DP is an exact oracle for the greedy.
    """
    levels = np.arange(tank + 1)
    dp = np.full(tank + 1, math.inf)
    dp[start_fuel] = 0.0
    for k in range(1, len(positions)):
        distance = positions[k] - positions[k - 1]
        arrive = np.full(tank + 1, math.inf)
        if distance <= tank:
            arrive[: tank + 1 - distance] = dp[distance:]
        if k == len(positions) - 1:
            return float(arrive.min())
        price = prices[k]
        # Buying any amount: dp[f] = min over g <= f of arrive[g] + (f - g) * price
        dp = np.minimum.accumulate(arrive - levels * price) + levels * price
    raise AssertionError("unreachable")


def greedy_cost(positions, prices, start_fuel, tank):
    return sum(bought * prices[node] for node, _, bought in _greedy(positions, prices, start_fuel, tank))


@pytest.mark.parametrize("seed", range(400))
def test_greedy_matches_exact_optimum_on_random_routes(seed):
    rng = random.Random(seed)
    tank = rng.choice([100, 250, 500])
    route = rng.randint(0, 4 * tank)
    count = rng.randint(0, 12)
    miles = sorted(rng.randint(0, route) for _ in range(count))
    station_prices = [round(rng.uniform(2.5, 4.5), rng.choice([1, 2, 3])) for _ in miles]
    start_fuel = rng.randint(0, tank)
    target = route + rng.choice([0, rng.randint(0, tank // 2)])
    positions = [0, *miles, target]
    prices = [math.inf, *station_prices, -math.inf]

    best = exact_min_cost(positions, prices, start_fuel, tank)
    if math.isinf(best):
        with pytest.raises(InfeasibleRouteError):
            _greedy(positions, prices, start_fuel, tank)
    else:
        assert greedy_cost(positions, prices, start_fuel, tank) == pytest.approx(best, abs=1e-6)


def best_objective_over_subsets(stations, route, tank, mpg, reserve, stop_cost):
    """Exact optimum with a per-stop cost: try every subset of stations as the stop set."""
    start = min(tank, max(reserve, stations[0].mile))
    target = min(route + start, stations[-1].mile + tank)
    if target < route:
        return math.inf  # the destination is out of reach of the last station
    best = math.inf
    for size in range(len(stations) + 1):
        for subset in itertools.combinations(stations, size):
            try:
                purchases = _purchases(list(subset), start, target, tank)
            except InfeasibleRouteError:
                continue
            fuel_cost = sum(bought / mpg * subset[node].price for node, _, bought in purchases)
            best = min(best, fuel_cost + stop_cost * len(purchases))
    return best


def assert_plan_is_feasible(plan, vehicle=TRUCK):
    """Drive the plan: the tank never runs dry or overflows, and fuel bought equals fuel burned."""
    tank, fuel, position = vehicle.tank_gallons, plan.start_fuel_gallons, 0.0
    for stop in plan.stops:
        fuel -= (stop.station.mile - position) / vehicle.mpg
        assert fuel >= -1e-9, "ran out of fuel before a stop"
        assert stop.arrival_fuel_gallons == pytest.approx(fuel, abs=1e-6)
        assert stop.gallons > 0
        fuel += stop.gallons
        assert fuel <= tank + 1e-9, "bought more than the tank holds"
        position = stop.station.mile
    fuel -= (plan.route_miles - position) / vehicle.mpg
    assert fuel == pytest.approx(plan.end_fuel_gallons, abs=1e-6)
    assert fuel >= -1e-9
    assert plan.total_gallons == pytest.approx(
        plan.fuel_burned_gallons + plan.end_fuel_gallons - plan.start_fuel_gallons, abs=1e-6
    )


def random_instance(rng, *, boundary=False, fractional=False):
    route = rng.randint(50, 1400)
    if boundary:  # legs of exactly one tank, where whole-mile rounding matters most
        miles = sorted(
            {min(route, rng.choice([0, 1, 2]) + 500 * k + rng.choice([0, 0.4, 0.6])) for k in range(4)}
        )
    elif fractional:
        miles = sorted({round(rng.uniform(0, route), 2) for _ in range(rng.randint(1, 8))})
    else:
        miles = sorted({rng.randint(0, route) for _ in range(rng.randint(1, 7))})
    stations = [station(i, mile, round(rng.uniform(2.6, 4.2), 2)) for i, mile in enumerate(miles)]
    return route, stations, rng.choice([1.0, 5.0, 15.0, 40.0]), rng.choice([0, 20, 50])


def objective(plan, stop_cost):
    return plan.total_cost + stop_cost * len(plan.stops)


@pytest.mark.parametrize("seed", range(200))
def test_stop_cost_plan_is_optimal_on_whole_mile_positions(seed):
    route, stations, stop_cost, reserve = random_instance(random.Random(seed))
    expected = best_objective_over_subsets(stations, route, 500, 10, reserve, stop_cost)
    try:
        plan = plan_fuel_stops(stations, route, TRUCK, reserve_miles=reserve, stop_cost=stop_cost)
    except InfeasibleRouteError:
        assert expected == math.inf
        return
    assert objective(plan, stop_cost) == pytest.approx(expected, abs=1e-6)
    assert_plan_is_feasible(plan)


@pytest.mark.parametrize("seed", range(300))
@pytest.mark.parametrize("kind", ["fractional", "boundary"])
def test_stop_cost_plan_is_near_optimal_and_never_worse_than_greedy(seed, kind):
    """Off the whole-mile grid the DP can be off by rounding, but it is never worse than
    the pure greedy and stays within cents of the exhaustive optimum."""
    route, stations, stop_cost, reserve = random_instance(random.Random(seed), **{kind: True})
    expected = best_objective_over_subsets(stations, route, 500, 10, reserve, stop_cost)
    try:
        plan = plan_fuel_stops(stations, route, TRUCK, reserve_miles=reserve, stop_cost=stop_cost)
    except InfeasibleRouteError:
        assert expected == math.inf
        return
    greedy = plan_fuel_stops(stations, route, TRUCK, reserve_miles=reserve, stop_cost=0)
    assert_plan_is_feasible(plan)
    assert objective(plan, stop_cost) <= objective(greedy, stop_cost) + 1e-6
    assert objective(plan, stop_cost) <= expected + 0.25


def test_station_just_inside_the_reserve_is_reachable():
    """Regression: a whole-mile margin used to hide a station at mile 49.7 of a 50-mile reserve."""
    stations = [station(1, 14.5, 3.90), station(2, 49.71, 3.00), station(3, 540, 3.00)]
    plan = plan_fuel_stops(stations, route_miles=900, vehicle=TRUCK, reserve_miles=50, stop_cost=15)
    assert [s.station.ref for s in plan.stops] == [2, 3]


def test_stop_cost_removes_stops_that_save_only_cents():
    stations = [station(1, 0, 3.10), station(2, 5, 3.09)]
    cheapest = plan_fuel_stops(stations, route_miles=300, vehicle=TRUCK, reserve_miles=0, stop_cost=0)
    practical = plan_fuel_stops(stations, route_miles=300, vehicle=TRUCK, reserve_miles=0, stop_cost=10)
    assert len(cheapest.stops) == 2
    assert len(practical.stops) == 1
    assert practical.total_cost - cheapest.total_cost < 0.5


def test_stop_cost_keeps_stops_that_pay_for_themselves():
    stations = [station(1, 0, 3.90), station(2, 40, 2.90)]
    plan = plan_fuel_stops(stations, route_miles=400, vehicle=TRUCK, reserve_miles=0, stop_cost=15)
    assert [s.station.ref for s in plan.stops] == [1, 2]
    assert plan.stops[0].gallons == pytest.approx(4.0)


def test_buys_just_enough_to_reach_a_cheaper_station():
    plan = plan_fuel_stops(
        [station(1, 0, 4.00), station(2, 100, 3.00)], route_miles=300, vehicle=TRUCK, reserve_miles=0
    )
    assert [(s.station.ref, round(s.gallons, 6)) for s in plan.stops] == [(1, 10.0), (2, 20.0)]
    assert plan.total_cost == pytest.approx(10 * 4.00 + 20 * 3.00)


def test_fills_up_when_no_cheaper_station_is_in_range():
    stations = [station(1, 0, 3.00), station(2, 450, 3.50), station(3, 600, 3.20)]
    plan = plan_fuel_stops(stations, route_miles=900, vehicle=TRUCK, reserve_miles=0)
    # Fill (50 gal) at the cheapest station, top up just enough at the dearer one to reach
    # the cheaper third station, then buy what's left to finish there.
    assert [(s.station.ref, round(s.gallons, 6)) for s in plan.stops] == [(1, 50.0), (2, 10.0), (3, 30.0)]
    assert plan.total_gallons == pytest.approx(90.0)


def test_trip_is_fuel_neutral_with_default_reserve():
    stations = [station(i, mile, 3.0 + (i % 3) / 10) for i, mile in enumerate(range(0, 2001, 120))]
    plan = plan_fuel_stops(stations, route_miles=2000, vehicle=TRUCK)
    assert plan.start_fuel_gallons == pytest.approx(5.0)
    assert plan.end_fuel_gallons == pytest.approx(5.0)
    assert plan.total_gallons == pytest.approx(plan.fuel_burned_gallons) == pytest.approx(200.0)


def test_reserve_lets_the_plan_skip_expensive_stations_near_the_start():
    stations = [station(1, 0, 4.50), station(2, 30, 3.00)]
    plan = plan_fuel_stops(stations, route_miles=200, vehicle=TRUCK, reserve_miles=50)
    assert [s.station.ref for s in plan.stops] == [2]
    assert plan.total_gallons == pytest.approx(20.0)


def test_start_fuel_covers_a_long_first_leg_without_stations():
    plan = plan_fuel_stops([station(1, 300, 3.0)], route_miles=600, vehicle=TRUCK, reserve_miles=50)
    assert plan.start_fuel_gallons == pytest.approx(30.0)
    assert plan.stops[0].arrival_fuel_gallons == pytest.approx(0.0)
    # A full tank bought at mile 300 reaches mile 800, so the vehicle can only arrive with
    # 200 miles (20 gal) of the 300-mile start fuel: it buys 60 - 30 + 20 = 50 gallons.
    assert plan.end_fuel_gallons == pytest.approx(20.0)
    assert plan.total_gallons == pytest.approx(50.0)


def test_route_without_stations_within_one_tank_needs_no_stops():
    plan = plan_fuel_stops([], route_miles=320, vehicle=TRUCK)
    assert plan.stops == ()
    assert plan.start_fuel_gallons == pytest.approx(32.0)


def test_route_without_stations_longer_than_range_is_infeasible():
    with pytest.raises(InfeasibleRouteError):
        plan_fuel_stops([], route_miles=650, vehicle=TRUCK)


def test_gap_longer_than_range_is_reported_with_its_location():
    with pytest.raises(InfeasibleRouteError) as error:
        plan_fuel_stops([station(1, 10, 3.0), station(2, 700, 3.0)], route_miles=900, vehicle=TRUCK)
    assert (error.value.gap_start_mile, error.value.gap_end_mile) == (10, 700)


def test_first_station_beyond_range_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as error:
        plan_fuel_stops([station(1, 520, 3.0)], route_miles=800, vehicle=TRUCK)
    assert error.value.gap_end_mile == 520


def test_last_station_too_far_from_destination_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as error:
        plan_fuel_stops([station(1, 0, 3.0)], route_miles=560, vehicle=TRUCK)
    assert (error.value.gap_start_mile, error.value.gap_end_mile) == (0, 560)


def test_arrival_reserve_is_reduced_when_the_last_station_is_far_from_the_destination():
    plan = plan_fuel_stops([station(1, 0, 3.0)], route_miles=480, vehicle=TRUCK, reserve_miles=50)
    assert plan.end_fuel_gallons == pytest.approx(2.0)  # tank allows 500 - 480 = 20 miles
    assert plan.total_gallons == pytest.approx(50.0 - 5.0)


def test_only_the_cheapest_of_colocated_stations_is_used():
    stations = [station(1, 0, 3.40), station(2, 0, 3.10), station(3, 0, 3.90)]
    plan = plan_fuel_stops(stations, route_miles=100, vehicle=TRUCK, reserve_miles=0)
    assert [s.station.ref for s in plan.stops] == [2]


def test_stations_outside_the_route_span_are_ignored():
    stations = [station(1, -5, 1.00), station(2, 0, 3.00), station(3, 150, 1.00)]
    plan = plan_fuel_stops(stations, route_miles=100, vehicle=TRUCK, reserve_miles=0)
    assert [s.station.ref for s in plan.stops] == [2]


def test_zero_length_route():
    plan = plan_fuel_stops([station(1, 0, 3.0)], route_miles=0, vehicle=TRUCK)
    assert plan.total_gallons == pytest.approx(0.0)


def test_vehicle_rejects_invalid_parameters():
    with pytest.raises(ValueError):
        Vehicle(range_miles=0, mpg=10)
