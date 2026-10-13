"""Turn user input into a US location.

Accepted inputs, cheapest first:
1. Coordinates: "41.8781,-87.6298" or {"lat": 41.8781, "lng": -87.6298}. No lookup at all.
2. "City, ST" / "City, State": resolved offline from the Census gazetteer. No API call.
3. Anything else (street addresses, landmarks, ZIP codes): one call to Nominatim
   (OpenStreetMap), cached so the same text is never looked up twice.
Every result must fall inside the USA outline.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass

import requests

from .errors import LocationError, UpstreamServiceError
from .gazetteer import Gazetteer, UsBoundary, names_non_us_region

logger = logging.getLogger(__name__)

_COORDINATES = re.compile(r"^\s*([-+]?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")


@dataclass(frozen=True, slots=True)
class Location:
    query: str
    label: str
    latitude: float
    longitude: float
    source: str  # "coordinates" | "gazetteer" | "nominatim"

    def as_dict(self) -> dict:
        return asdict(self)


class NominatimClient:
    """Free-text geocoder (OpenStreetMap Nominatim). Its usage policy allows one request
    per second, so calls are spaced out process-wide."""

    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(
        self, base_url: str, *, timeout: float, user_agent: str, session: requests.Session | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def search(self, query: str) -> tuple[float, float, str] | None:
        params = {"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
        with NominatimClient._lock:
            wait = 1.0 - (time.monotonic() - NominatimClient._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(
                    f"{self.base_url}/search", params=params, timeout=(5, self.timeout)
                )
            except requests.RequestException as exc:
                raise UpstreamServiceError("The geocoding service could not be reached.") from exc
            finally:
                NominatimClient._last_call = time.monotonic()
        if response.status_code != 200:
            logger.warning("Nominatim returned HTTP %s for %r", response.status_code, query)
            raise UpstreamServiceError("The geocoding service returned an error.")
        try:
            results = response.json()
        except ValueError as exc:
            raise UpstreamServiceError("The geocoding service returned an invalid response.") from exc
        if not results:
            return None
        hit = results[0]
        return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query)


class Geocoder:
    def __init__(
        self, *, gazetteer: Gazetteer, boundary: UsBoundary, nominatim: NominatimClient, cache, cache_seconds
    ):
        self.gazetteer = gazetteer
        self.boundary = boundary
        self.nominatim = nominatim
        self.cache = cache
        self.cache_seconds = cache_seconds

    def resolve(self, value: str | dict, *, field: str) -> tuple[Location, int]:
        """Return the location and the number of external API calls it took (0 or 1)."""
        if isinstance(value, dict):
            return self._from_coordinates(
                value["lat"], value["lng"], f"{value['lat']},{value['lng']}", field
            ), 0
        query = " ".join(value.split())
        if match := _COORDINATES.match(query):
            return self._from_coordinates(float(match[1]), float(match[2]), query, field), 0
        if place := self.gazetteer.resolve(query):
            return Location(query, place.label, place.latitude, place.longitude, "gazetteer"), 0
        if names_non_us_region(query):
            raise LocationError(
                f"'{query}' is outside the USA. Start and finish must both be in the United States.",
                code="outside_usa",
                details={"field": field},
            )
        return self._from_nominatim(query, field)

    def _from_coordinates(self, lat: float, lng: float, query: str, field: str) -> Location:
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise LocationError(
                f"'{query}' is not a valid latitude,longitude pair.",
                details={"field": field},
            )
        self._require_usa(lat, lng, query, field)
        return Location(query, f"{lat:.5f}, {lng:.5f}", lat, lng, "coordinates")

    def _from_nominatim(self, query: str, field: str) -> tuple[Location, int]:
        key = "geocode:v1:" + hashlib.sha1(query.lower().encode()).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            if cached == "not-found":
                self._not_found(query, field)
            location = Location(**{**cached, "query": query})
            self._require_usa(location.latitude, location.longitude, query, field)
            return location, 0
        hit = self.nominatim.search(query)
        if hit is None:
            self.cache.set(key, "not-found", 3600)
            self._not_found(query, field)
        lat, lng, label = hit
        location = Location(query, label, lat, lng, "nominatim")
        self.cache.set(key, location.as_dict(), self.cache_seconds)  # cached even if outside the USA
        self._require_usa(lat, lng, query, field)
        return location, 1

    def _require_usa(self, lat: float, lng: float, query: str, field: str) -> None:
        if not self.boundary.contains(lat, lng):
            raise LocationError(
                f"'{query}' is outside the USA. Start and finish must both be in the United States "
                "(coordinates are expected as latitude,longitude).",
                code="outside_usa",
                details={"field": field, "latitude": lat, "longitude": lng},
            )

    @staticmethod
    def _not_found(query: str, field: str):
        raise LocationError(
            f"Could not find a US location for '{query}'. Try 'City, ST', a full address or 'lat,lng'.",
            code="location_not_found",
            details={"field": field},
        )
