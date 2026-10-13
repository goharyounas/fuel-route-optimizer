"""Cost-optimal refuelling along a fixed route (no Django dependencies).

Problem
-------
A vehicle drives a fixed route of ``route_miles``. Fuel stations sit at known mile
markers along the route, each with its own price per gallon. The tank holds enough
fuel for ``range_miles``. Choose where to stop and how much to buy so that fuel cost
plus a fixed ``stop_cost`` per stop is as small as possible. The stop cost stands
for the driver's time. Without it the cheapest plan buys a gallon or two at every
slightly cheaper station: optimal on paper, absurd on the road.

Algorithm
---------
1. **Choose the stops**, by dynamic programming over (station, fuel level). Fuel is
   discretised to one mile of range (0.1 gal at 10 mpg). At each station the vehicle
   either drives past, or stops (paying ``stop_cost``) and buys any amount. A prefix
   minimum makes the "buy any amount" transition O(levels), so the whole DP is
   O(stations x levels), vectorised with numpy.
2. **Choose the amounts**, exactly, with the classic greedy, which is optimal for a
   fixed set of stations (Khuller, Malekian & Mestre, "To fill or not to fill: the gas
   station problem", 2007). At each chosen station: if a cheaper one is within a full
   tank's reach, buy just enough to get to the first such station. Otherwise fill the
   tank and go to the cheapest station in range.

The DP runs on a whole-mile grid, where it is exact. Rounding can make a leg near the
tank's limit look slightly shorter or longer than it is, so the DP runs twice: as is,
and with a one-mile safety margin. Each stop set it proposes, plus "all stations"
(the pure greedy), is then priced exactly, and the cheapest feasible one wins. With
``stop_cost=0`` only the greedy runs, which gives the minimum fuel bill.

Cost model
----------
The trip is *fuel neutral*: the vehicle starts with a reserve of fuel and must finish
with the same reserve, so it buys exactly the fuel the trip burns
(``route_miles / mpg``). The reported total is therefore the trip's full fuel cost,
and the reserve lets the plan skip expensive stations right next to the start. The
reserve defaults to ``reserve_miles``. It is raised when the first station on the route
is farther away (e.g. a start in a region without stations in the dataset), and
lowered when the tank cannot carry it past the last station.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Vehicle:
    range_miles: float
    mpg: float

    def __post_init__(self):
        if self.range_miles <= 0 or self.mpg <= 0:
            raise ValueError("Vehicle range and mpg must be positive.")

    @property
    def tank_gallons(self) -> float:
        return self.range_miles / self.mpg


@dataclass(frozen=True, slots=True)
class CandidateStation:
    """A fuel station projected onto the route."""

    ref: int  # caller-defined identifier, e.g. an index into the station index
    mile: float  # distance from the start of the route, in miles
    price: float  # USD per gallon


@dataclass(frozen=True, slots=True)
class FuelStop:
    station: CandidateStation
    arrival_fuel_gallons: float
    gallons: float

    @property
    def cost(self) -> float:
        return self.gallons * self.station.price


@dataclass(frozen=True, slots=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    route_miles: float
    start_fuel_gallons: float
    end_fuel_gallons: float
    fuel_burned_gallons: float

    @property
    def total_gallons(self) -> float:
        return sum(stop.gallons for stop in self.stops)

    @property
    def total_cost(self) -> float:
        return sum(stop.cost for stop in self.stops)


class InfeasibleRouteError(Exception):
    """No refuelling plan exists: some stretch of the route has no station within range."""

    def __init__(self, message: str, *, gap_start_mile: float, gap_end_mile: float):
        super().__init__(message)
        self.gap_start_mile = gap_start_mile
        self.gap_end_mile = gap_end_mile


def plan_fuel_stops(
    stations: Iterable[CandidateStation],
    route_miles: float,
    vehicle: Vehicle,
    *,
    reserve_miles: float = 50.0,
    stop_cost: float = 0.0,
) -> FuelPlan:
    """Return the cheapest fuel-neutral refuelling plan for the route.

    ``stop_cost`` (USD) is added to the objective for every stop, but not to the
    reported fuel cost. Raises InfeasibleRouteError when a stretch longer than the
    vehicle's range has no station on it.
    """
    if route_miles < 0:
        raise ValueError("route_miles must be non-negative.")
    if stop_cost < 0:
        raise ValueError("stop_cost must be non-negative.")
    tank = vehicle.range_miles
    nodes = _prepare_stations(stations, route_miles)

    if not nodes:
        if route_miles > tank + _EPS:
            raise InfeasibleRouteError(
                f"No fuel stations were found along this {route_miles:.0f}-mile route, "
                f"which is longer than the vehicle's {tank:.0f}-mile range.",
                gap_start_mile=0.0,
                gap_end_mile=route_miles,
            )
        burned = route_miles / vehicle.mpg
        return FuelPlan((), route_miles, burned, 0.0, burned)

    first_mile, last_mile = nodes[0].mile, nodes[-1].mile
    if first_mile > tank + _EPS:
        raise InfeasibleRouteError(
            f"The first fuel station on the route is {first_mile:.0f} miles from the start, "
            f"beyond the vehicle's {tank:.0f}-mile range.",
            gap_start_mile=0.0,
            gap_end_mile=first_mile,
        )
    start_fuel = min(tank, max(reserve_miles, first_mile))
    # Fuel neutral: plan to arrive with the fuel we started with. If the last station is
    # too far from the destination for that, arrive with as much fuel as the tank allows.
    target = min(route_miles + start_fuel, last_mile + tank)
    if target < route_miles - _EPS:
        raise InfeasibleRouteError(
            f"The last fuel station on the route is {route_miles - last_mile:.0f} miles before "
            f"the destination, beyond the vehicle's {tank:.0f}-mile range.",
            gap_start_mile=last_mile,
            gap_end_mile=route_miles,
        )

    # Candidate stop sets: every station (the greedy then gives the cheapest fuel bill),
    # plus the DP's picks on a whole-mile grid, with and without a one-mile safety margin.
    # Each candidate is priced exactly and the lowest fuel cost + stop cost wins, so the
    # result is never worse than the pure greedy, and rounding can't produce a plan that
    # runs dry.
    candidates = [nodes]
    if stop_cost > 0:
        for slack in (0, 1):
            dp_target = min(target, last_mile + tank - slack)
            chosen = _select_stops(nodes, start_fuel, dp_target, tank, vehicle.mpg, stop_cost, slack=slack)
            if chosen:
                candidates.append(chosen)
    best = None
    for candidate in candidates:
        try:
            purchases = _purchases(candidate, start_fuel, target, tank)
        except InfeasibleRouteError:
            if candidate is nodes:
                raise  # no plan exists even with every station available
            continue
        objective = sum(bought / vehicle.mpg * candidate[node].price for node, _, bought in purchases)
        objective += stop_cost * len(purchases)
        if best is None or objective < best[0] - 1e-9:
            best = (objective, candidate, purchases)
    _, nodes, purchases = best

    stops = tuple(
        FuelStop(
            station=nodes[node], arrival_fuel_gallons=arrival / vehicle.mpg, gallons=bought / vehicle.mpg
        )
        for node, arrival, bought in purchases
    )
    return FuelPlan(
        stops=stops,
        route_miles=route_miles,
        start_fuel_gallons=start_fuel / vehicle.mpg,
        end_fuel_gallons=(target - route_miles) / vehicle.mpg,
        fuel_burned_gallons=route_miles / vehicle.mpg,
    )


def _prepare_stations(stations: Iterable[CandidateStation], route_miles: float) -> list[CandidateStation]:
    """Sort stations along the route and keep only the cheapest at any given mile marker."""
    ordered = sorted(
        (s for s in stations if -_EPS <= s.mile <= route_miles + _EPS and s.price > 0),
        key=lambda s: (s.mile, s.price),
    )
    unique: list[CandidateStation] = []
    for station in ordered:
        if unique and abs(station.mile - unique[-1].mile) < 1e-6:
            continue  # same position, more expensive (list is sorted by price within a mile)
        unique.append(station)
    return unique


def _purchases(nodes: list[CandidateStation], start_fuel: float, target: float, tank: float):
    """Optimal purchase amounts at ``nodes``: list of (node index, fuel on arrival, fuel bought)."""
    raw = _greedy(
        positions=[0.0, *(n.mile for n in nodes), target],
        prices=[math.inf, *(n.price for n in nodes), -math.inf],
        start_fuel=start_fuel,
        tank=tank,
    )
    return [(node - 1, arrival, bought) for node, arrival, bought in raw]


def _select_stops(
    nodes: list[CandidateStation],
    start_fuel: float,
    target: float,
    tank: float,
    mpg: float,
    stop_cost: float,
    *,
    slack: int,
) -> list[CandidateStation] | None:
    """Stations at which an optimal plan stops, given a fixed cost per stop.

    DP over fuel levels in whole miles of range. ``best[f]`` is the minimum cost of being
    at the current station with ``f`` miles of fuel after deciding whether to stop. It is
    exact when positions are whole miles. Otherwise rounding can make a leg look slightly
    shorter or longer than it is. ``slack=1`` plans with a tank one mile smaller and the
    start fuel rounded down, so every leg it accepts also fits at the exact positions.
    Returns None when no plan exists on the grid.
    """
    top = math.floor(tank + _EPS) - slack
    if top < 1:
        return None
    levels = np.arange(top + 1)
    positions = np.array([round(n.mile) for n in nodes] + [round(target)], dtype=int)
    stopped = np.zeros((len(nodes), levels.size), dtype=bool)
    arrived_with = np.zeros((len(nodes), levels.size), dtype=np.int32)

    # If the start fuel barely reaches the first station, the vehicle arrives there
    # (nearly) empty and must stop.
    forced_first_stop = slack and start_fuel - nodes[0].mile < 1.0
    if forced_first_stop:
        arrive = np.full(levels.size, np.inf)
        arrive[0] = 0.0
    else:
        start = np.full(levels.size, np.inf)
        start[min(top, math.floor(start_fuel - 0.5) if slack else round(start_fuel))] = 0.0
        arrive = _drive(start, positions[0])

    for k, node in enumerate(nodes):
        unit_price = node.price / mpg  # cost of one mile of range
        # Stopping to buy b >= 1 miles of fuel, arriving with g = f - b:
        #   cost(f) = stop_cost + min over g < f of (arrive[g] + (f - g) * unit_price)
        shifted = arrive - levels * unit_price
        running = np.minimum.accumulate(shifted)
        argmin = np.maximum.accumulate(np.where(shifted == running, levels, 0))
        stop = np.full(levels.size, np.inf)
        stop[1:] = running[:-1] + levels[1:] * unit_price + stop_cost
        stopped[k] = True if (forced_first_stop and k == 0) else stop < arrive
        arrived_with[k, 1:] = argmin[:-1]
        best = np.where(stopped[k], stop, arrive)
        arrive = _drive(best, positions[k + 1] - positions[k])  # at the next station (or the target)

    finish = arrive
    if not np.isfinite(finish.min()):
        return None
    # Walk back from the destination, recovering where the vehicle stopped.
    fuel = int(np.argmin(finish))
    chosen: list[int] = []
    for k in range(len(nodes) - 1, -1, -1):
        fuel += positions[k + 1] - positions[k]  # fuel when leaving station k
        if fuel > top:
            return None
        if stopped[k, fuel]:
            chosen.append(k)
            fuel = int(arrived_with[k, fuel])
    return [nodes[k] for k in reversed(chosen)]


def _drive(fuel_costs: np.ndarray, distance: int) -> np.ndarray:
    """Shift a fuel-level cost vector by the fuel burned driving ``distance`` miles."""
    arrive = np.full(fuel_costs.size, np.inf)
    if distance < fuel_costs.size:
        arrive[: fuel_costs.size - distance] = fuel_costs[distance:]
    return arrive


def _greedy(positions: list[float], prices: list[float], start_fuel: float, tank: float):
    """Optimal purchases for a fixed set of nodes [origin, stations..., target].

    Node 0 is the origin, where fuel cannot be bought (price +inf). The last node is the
    target (price -inf, so fuel is never carried past it). Fuel quantities are in miles
    of range. Returns (node_index, fuel_on_arrival, fuel_bought) for each purchase.
    """
    last = len(positions) - 1
    purchases: list[tuple[int, float, float]] = []
    current, fuel = 0, start_fuel
    while current < last:
        here, price_here = positions[current], prices[current]
        reach = fuel if current == 0 else tank
        cheaper = cheapest = None
        candidate = current + 1
        while candidate <= last and positions[candidate] - here <= reach + _EPS:
            if prices[candidate] < price_here:
                cheaper = candidate
                break
            if cheapest is None or prices[candidate] <= prices[cheapest]:
                cheapest = candidate  # "<=" so ties resolve to the farthest station
            candidate += 1

        if cheaper is not None:
            needed = positions[cheaper] - here
            if needed > fuel + _EPS:
                purchases.append((current, fuel, needed - fuel))
                fuel = needed
            fuel = max(fuel - needed, 0.0)
            current = cheaper
            continue

        if cheapest is None:
            raise InfeasibleRouteError(
                f"No fuel station between mile {here:.0f} and mile {positions[current + 1]:.0f} "
                f"of the route: the gap exceeds the vehicle's {tank:.0f}-mile range.",
                gap_start_mile=here,
                gap_end_mile=positions[current + 1],
            )
        if tank - fuel > _EPS:
            purchases.append((current, fuel, tank - fuel))
        fuel = max(tank - (positions[cheapest] - here), 0.0)
        current = cheapest
    return purchases
