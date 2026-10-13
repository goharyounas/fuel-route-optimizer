"""Client for the OSRM routing API (https://project-osrm.org).

The public demo server needs no API key. One request returns the route's distance,
duration and full geometry, which is everything the planner needs. Point
OSRM_BASE_URL at a self-hosted instance for production traffic.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .errors import RouteNotFoundError, UpstreamServiceError

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
# OSRM error codes meaning "these points can't be connected", as opposed to a server fault.
_NO_ROUTE_CODES = {"NoRoute", "NoSegment", "NoMatch", "NoTrips"}
_RETRYABLE_STATUS = {429, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class RouteData:
    """Compact, cacheable routing result."""

    distance_meters: float
    duration_seconds: float
    polyline: str  # Google encoded polyline, precision 6
    polyline_precision: int = 6

    @property
    def distance_miles(self) -> float:
        return self.distance_meters / METERS_PER_MILE

    def as_dict(self) -> dict:
        return {
            "distance_meters": self.distance_meters,
            "duration_seconds": self.duration_seconds,
            "polyline": self.polyline,
            "polyline_precision": self.polyline_precision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RouteData:
        return cls(**data)


class OsrmClient:
    provider = "osrm"

    def __init__(
        self, base_url: str, *, timeout: float, user_agent: str, session: requests.Session | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()  # keep-alive between requests
        self.session.headers["User-Agent"] = user_agent

    def route(self, start: tuple[float, float], finish: tuple[float, float]) -> tuple[RouteData, int]:
        """Return the fastest driving route between two (lat, lon) points and the HTTP calls made."""
        (lat1, lon1), (lat2, lon2) = start, finish
        url = f"{self.base_url}/route/v1/driving/{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
        params = {"overview": "full", "geometries": "polyline6", "steps": "false", "alternatives": "false"}

        calls = 0
        for attempt in (1, 2):
            calls += 1
            try:
                response = self.session.get(url, params=params, timeout=(5, self.timeout))
            except requests.ConnectionError as exc:
                if attempt == 1:
                    time.sleep(0.5)
                    continue
                raise UpstreamServiceError("The routing service could not be reached.") from exc
            except requests.Timeout as exc:
                raise UpstreamServiceError("The routing service timed out.") from exc
            except requests.RequestException as exc:
                raise UpstreamServiceError("The routing request failed.") from exc
            if response.status_code in _RETRYABLE_STATUS and attempt == 1:
                time.sleep(1.0)
                continue
            return self._parse(response), calls
        raise UpstreamServiceError("The routing service is unavailable.")  # pragma: no cover

    def _parse(self, response: requests.Response) -> RouteData:
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamServiceError(f"Routing service returned HTTP {response.status_code}.") from exc
        code = payload.get("code")
        if code in _NO_ROUTE_CODES:
            raise RouteNotFoundError(
                "No drivable route connects these locations.", details={"provider_code": code}
            )
        if response.status_code != 200 or code != "Ok" or not payload.get("routes"):
            logger.warning("OSRM error %s: %s", response.status_code, payload.get("message"))
            raise UpstreamServiceError(
                "The routing service returned an error.",
                details={"provider_code": code, "provider_message": payload.get("message")},
            )
        route = payload["routes"][0]
        return RouteData(
            distance_meters=float(route["distance"]),
            duration_seconds=float(route["duration"]),
            polyline=route["geometry"],
        )
