"""Builds a fuel plan: geocode -> route -> stations on the route -> optimise -> response.

External calls per request:
* routing (OSRM): one call for a new start/finish pair, none when the route is cached;
* geocoding (Nominatim): only for free-text input that isn't "City, ST" or coordinates,
  and never twice for the same text.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .errors import LocationError, NoFeasiblePlanError, StationDataMissingError
from .gazetteer import get_gazetteer, get_us_boundary
from .geo import decode_polyline, haversine_miles, simplify_polyline
from .geocoding import Geocoder, Location, NominatimClient
from .optimizer import CandidateStation, FuelPlan, InfeasibleRouteError, Vehicle, plan_fuel_stops
from .routing import OsrmClient, RouteData
from .stations import RouteMatch, StationIndex, get_station_index

logger = logging.getLogger(__name__)

# Bump when the response layout or planning logic changes, to invalidate cached plans.
PLAN_CACHE_VERSION = 1
_STOP_MAP_PROPERTIES = ("stop", "name", "city", "state", "price_per_gallon", "gallons", "cost", "route_mile")


@dataclass(frozen=True, slots=True)
class PlanResult:
    payload: dict  # API response body
    stations_on_route: list[dict]  # every station the plan could use (shown on the HTML map)


class RoutePlanner:
    def __init__(self, *, geocoder: Geocoder, router: OsrmClient, cache, config: dict):
        self.geocoder = geocoder
        self.router = router
        self.cache = cache
        self.config = config
        self.vehicle = Vehicle(config["VEHICLE_RANGE_MILES"], config["VEHICLE_MPG"])

    def plan(self, start: str | dict, finish: str | dict, *, stop_cost: float | None = None) -> PlanResult:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        calls = {"routing": 0, "geocoding": 0}

        origin, geocode_calls_a = self.geocoder.resolve(start, field="start")
        destination, geocode_calls_b = self.geocoder.resolve(finish, field="finish")
        calls["geocoding"] = geocode_calls_a + geocode_calls_b
        timings["geocode"] = _ms_since(started)
        if (
            haversine_miles(origin.latitude, origin.longitude, destination.latitude, destination.longitude)
            < 0.1
        ):
            raise LocationError("Start and finish are the same place.", code="same_location")

        stop_cost = self.config["STOP_COST_USD"] if stop_cost is None else stop_cost
        index = get_station_index()
        if not len(index):
            raise StationDataMissingError(
                "No fuel station data is loaded. Run `python manage.py load_stations`."
            )

        plan_key = self._plan_key(origin, destination, stop_cost, index.version)
        cached = self.cache.get(plan_key)
        cache_status = {"plan": "hit" if cached else "miss", "route": "not needed" if cached else "miss"}
        if cached is None:
            cached = self._compute(origin, destination, stop_cost, index, calls, timings, cache_status)
            self.cache.set(plan_key, cached, self.config["PLAN_CACHE_SECONDS"])

        payload = {
            "start": origin.as_dict(),
            "finish": destination.as_dict(),
            **cached["payload"],
            "meta": {
                "cache": cache_status,
                "external_api_calls": calls,
                "timings_ms": {**timings, "total": _ms_since(started)},
                "station_data_version": index.version,
            },
        }
        return PlanResult(payload=payload, stations_on_route=cached["stations_on_route"])

    # ---------------------------------------------------------------------------------

    def _compute(self, origin, destination, stop_cost, index, calls, timings, cache_status) -> dict:
        step = time.perf_counter()
        route, route_calls, route_cached = self._route(origin, destination)
        calls["routing"] = route_calls
        cache_status["route"] = "hit" if route_cached else "miss"
        timings["route"] = _ms_since(step)

        step = time.perf_counter()
        points = decode_polyline(route.polyline, route.polyline_precision)
        latitudes, longitudes = points[:, 0], points[:, 1]
        matches = index.match_route(
            latitudes,
            longitudes,
            route.distance_miles,
            corridor_miles=self.config["ROUTE_CORRIDOR_MILES"],
            spacing_miles=self.config["ROUTE_SAMPLE_SPACING_MILES"],
        )
        timings["match_stations"] = _ms_since(step)

        step = time.perf_counter()
        try:
            plan = plan_fuel_stops(
                [CandidateStation(m.index, m.route_mile, float(index.prices[m.index])) for m in matches],
                route.distance_miles,
                self.vehicle,
                reserve_miles=self.config["FUEL_RESERVE_MILES"],
                stop_cost=stop_cost,
            )
        except InfeasibleRouteError as exc:
            raise NoFeasiblePlanError(
                str(exc),
                details={
                    "gap_start_mile": round(exc.gap_start_mile, 1),
                    "gap_end_mile": round(exc.gap_end_mile, 1),
                    "route_distance_miles": round(route.distance_miles, 1),
                    "stations_on_route": len(matches),
                },
            ) from exc
        timings["optimize"] = _ms_since(step)

        step = time.perf_counter()
        result = {
            "payload": self._payload(
                route=route,
                latitudes=latitudes,
                longitudes=longitudes,
                matches=matches,
                plan=plan,
                index=index,
                stop_cost=stop_cost,
                origin=origin,
                destination=destination,
            ),
            "stations_on_route": self._stations_on_route(matches, plan, index),
        }
        timings["build_response"] = _ms_since(step)
        return result

    def _route(self, origin: Location, destination: Location) -> tuple[RouteData, int, bool]:
        key = (
            f"route:v1:{self.router.provider}:{origin.latitude:.5f},{origin.longitude:.5f}:"
            f"{destination.latitude:.5f},{destination.longitude:.5f}"
        )
        if (cached := self.cache.get(key)) is not None:
            return RouteData.from_dict(cached), 0, True
        route, calls = self.router.route(
            (origin.latitude, origin.longitude), (destination.latitude, destination.longitude)
        )
        self.cache.set(key, route.as_dict(), self.config["ROUTE_CACHE_SECONDS"])
        return route, calls, False

    def _plan_key(self, origin: Location, destination: Location, stop_cost: float, data_version: str) -> str:
        settings_used = "|".join(
            str(self.config[name])
            for name in (
                "VEHICLE_RANGE_MILES",
                "VEHICLE_MPG",
                "FUEL_RESERVE_MILES",
                "ROUTE_CORRIDOR_MILES",
                "ROUTE_SAMPLE_SPACING_MILES",
                "MAP_SIMPLIFY_TOLERANCE_METERS",
            )
        )
        fingerprint = hashlib.sha1(f"{settings_used}|{stop_cost:.4f}".encode()).hexdigest()[:12]
        return (
            f"plan:v{PLAN_CACHE_VERSION}:{data_version}:{fingerprint}:{self.router.provider}:"
            f"{origin.latitude:.5f},{origin.longitude:.5f}:{destination.latitude:.5f},{destination.longitude:.5f}"
        )

    def _payload(
        self,
        *,
        route: RouteData,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        matches: list[RouteMatch],
        plan: FuelPlan,
        index: StationIndex,
        stop_cost: float,
        origin: Location,
        destination: Location,
    ) -> dict:
        by_ref = {m.index: m for m in matches}
        stops = []
        for number, stop in enumerate(plan.stops, start=1):
            station = index.stations[stop.station.ref]
            stops.append(
                {
                    "stop": number,
                    "opis_id": station.opis_id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "latitude": round(station.latitude, 5),
                    "longitude": round(station.longitude, 5),
                    "route_mile": round(stop.station.mile, 1),
                    "off_route_miles": round(by_ref[stop.station.ref].off_route_miles, 1),
                    "price_per_gallon": round(stop.station.price, 3),
                    "fuel_on_arrival_gallons": round(stop.arrival_fuel_gallons, 2),
                    "gallons": round(stop.gallons, 3),
                    "cost": round(stop.cost, 2),
                }
            )
        total_cost = round(sum(stop["cost"] for stop in stops), 2)
        total_gallons = plan.total_gallons
        reserve = round(self.config["FUEL_RESERVE_MILES"] / self.vehicle.mpg, 1)
        return {
            "route": {
                "distance_miles": round(route.distance_miles, 1),
                "duration_hours": round(route.duration_seconds / 3600, 2),
                "provider": self.router.provider,
            },
            "vehicle": {
                "max_range_miles": self.vehicle.range_miles,
                "mpg": self.vehicle.mpg,
                "tank_capacity_gallons": round(self.vehicle.tank_gallons, 2),
            },
            "fuel_stops": stops,
            "summary": {
                "total_fuel_cost": total_cost,
                "total_gallons_purchased": round(total_gallons, 3),
                "fuel_burned_gallons": round(plan.fuel_burned_gallons, 3),
                "average_price_per_gallon": round(total_cost / total_gallons, 3) if total_gallons else None,
                "number_of_stops": len(stops),
                "start_fuel_gallons": round(plan.start_fuel_gallons, 2),
                "end_fuel_gallons": round(plan.end_fuel_gallons, 2),
                "stations_on_route": len(matches),
                "stop_cost_usd": stop_cost,
            },
            "map": {"geojson": self._geojson(latitudes, longitudes, stops, route, origin, destination)},
            "assumptions": [
                f"Vehicle range {self.vehicle.range_miles:g} miles at {self.vehicle.mpg:g} mpg "
                f"({self.vehicle.tank_gallons:g}-gallon tank).",
                self._fuel_assumption(plan, reserve),
                f"Stops are chosen to minimise fuel cost plus ${stop_cost:g} per stop (driver time); pass "
                "stop_cost=0 for the absolute cheapest fuel bill.",
                f"A station counts as on the route when it is within {self.config['ROUTE_CORRIDOR_MILES']:g} "
                "miles of it. Station coordinates are city-level; each station uses its lowest listed price.",
            ],
        }

    @staticmethod
    def _fuel_assumption(plan: FuelPlan, reserve: float) -> str:
        start, end = plan.start_fuel_gallons, plan.end_fuel_gallons
        if end >= start - 0.005:
            return (
                f"Fuel-neutral trip: the vehicle starts with {start:.1f} gallons in the tank "
                f"(a {reserve:g}-gallon reserve, or more if the first station is farther away) and "
                "finishes with the same amount, so the total cost covers all fuel burned on the trip."
            )
        return (
            f"The vehicle starts with {start:.1f} gallons in the tank. The last station is too far from "
            f"the destination to finish with that much, so it arrives with {end:.1f} gallons; the total "
            "cost covers the fuel bought on the trip, not the fuel it started with."
        )

    def _geojson(self, latitudes, longitudes, stops, route, origin, destination) -> dict:
        keep = simplify_polyline(latitudes, longitudes, self.config["MAP_SIMPLIFY_TOLERANCE_METERS"])
        line = np.column_stack((longitudes[keep], latitudes[keep])).round(5).tolist()
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": {
                    "kind": "route",
                    "distance_miles": round(route.distance_miles, 1),
                    "duration_hours": round(route.duration_seconds / 3600, 2),
                },
            },
            _point(origin.longitude, origin.latitude, kind="start", label=origin.label),
            _point(destination.longitude, destination.latitude, kind="finish", label=destination.label),
        ]
        for stop in stops:
            properties = {key: stop[key] for key in _STOP_MAP_PROPERTIES}
            features.append(_point(stop["longitude"], stop["latitude"], kind="fuel_stop", **properties))
        return {"type": "FeatureCollection", "features": features}

    @staticmethod
    def _stations_on_route(matches: list[RouteMatch], plan: FuelPlan, index: StationIndex) -> list[dict]:
        chosen = {stop.station.ref for stop in plan.stops}
        rows = []
        for match in sorted(matches, key=lambda m: m.route_mile):
            station = index.stations[match.index]
            rows.append(
                {
                    "name": station.name,
                    "city": station.city,
                    "state": station.state,
                    "price": round(station.price, 3),
                    "route_mile": round(match.route_mile, 1),
                    "lat": round(station.latitude, 5),
                    "lng": round(station.longitude, 5),
                    "chosen": match.index in chosen,
                }
            )
        return rows


def _point(lng: float, lat: float, **properties) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": properties,
    }


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


@lru_cache(maxsize=1)
def get_planner() -> RoutePlanner:
    """Process-wide planner, built from settings (HTTP sessions are reused between requests)."""
    config = settings.FUEL_PLANNER
    user_agent, timeout = config["HTTP_USER_AGENT"], config["HTTP_TIMEOUT_SECONDS"]
    geocoder = Geocoder(
        gazetteer=get_gazetteer(),
        boundary=get_us_boundary(),
        nominatim=NominatimClient(config["NOMINATIM_BASE_URL"], timeout=timeout, user_agent=user_agent),
        cache=cache,
        cache_seconds=config["GEOCODE_CACHE_SECONDS"],
    )
    router = OsrmClient(config["OSRM_BASE_URL"], timeout=timeout, user_agent=user_agent)
    return RoutePlanner(geocoder=geocoder, router=router, cache=cache, config=config)
