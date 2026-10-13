"""In-memory spatial index of fuel stations, built once per process.

~6.7k stations fit comfortably in a few numpy arrays, so matching stations to a route
takes milliseconds and never touches the database on the request path.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .geo import chord_to_miles, miles_to_chord, resample_polyline, to_unit_vectors

_MILES_PER_DEGREE_LATITUDE = 69.05


@dataclass(frozen=True, slots=True)
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class RouteMatch:
    index: int  # position in StationIndex.stations
    route_mile: float  # distance from the route start to the closest point of the route
    off_route_miles: float  # straight-line distance from that point to the station


class StationIndex:
    def __init__(self, stations: list[Station]):
        self.stations = stations
        self.latitudes = np.array([s.latitude for s in stations], dtype=float)
        self.longitudes = np.array([s.longitude for s in stations], dtype=float)
        self.prices = np.array([s.price for s in stations], dtype=float)
        self.vectors = to_unit_vectors(self.latitudes, self.longitudes) if stations else np.empty((0, 3))
        digest = hashlib.sha1()
        for s in stations:
            digest.update(f"{s.opis_id}|{s.price:.6f}|{s.latitude:.5f}|{s.longitude:.5f};".encode())
        # Changes whenever the station data changes; used in cache keys.
        self.version = digest.hexdigest()[:12]

    def __len__(self) -> int:
        return len(self.stations)

    def match_route(
        self,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        route_miles: float,
        *,
        corridor_miles: float,
        spacing_miles: float,
    ) -> list[RouteMatch]:
        """Return the stations within ``corridor_miles`` of the route, with their mile marker.

        The route is resampled every ``spacing_miles`` and indexed in a KD-tree of 3D unit
        vectors. Each candidate station is then a single nearest-neighbour query, which
        is exact on the sphere and needs no map projection.
        """
        if not self.stations or latitudes.size == 0:
            return []
        lat_s, lon_s, along = resample_polyline(latitudes, longitudes, spacing_miles)

        # Cheap bounding-box prefilter before the exact search.
        pad_lat = corridor_miles / _MILES_PER_DEGREE_LATITUDE
        widest = np.radians(min(89.0, float(np.abs(lat_s).max()) + pad_lat))
        pad_lon = pad_lat / max(np.cos(widest), 1e-6)
        candidates = np.flatnonzero(
            (self.latitudes >= lat_s.min() - pad_lat)
            & (self.latitudes <= lat_s.max() + pad_lat)
            & (self.longitudes >= lon_s.min() - pad_lon)
            & (self.longitudes <= lon_s.max() + pad_lon)
        )
        if candidates.size == 0:
            return []

        tree = cKDTree(to_unit_vectors(lat_s, lon_s))
        chords, nearest = tree.query(
            self.vectors[candidates], distance_upper_bound=miles_to_chord(corridor_miles)
        )
        found = np.isfinite(chords)
        # Distances along the geometry can differ slightly from the routing engine's road
        # distance; scale so mile markers agree with the reported route length.
        scale = route_miles / along[-1] if along[-1] > 0 else 1.0
        return [
            RouteMatch(
                index=int(i), route_mile=float(along[n] * scale), off_route_miles=float(chord_to_miles(c))
            )
            for i, n, c in zip(candidates[found], nearest[found], chords[found], strict=True)
        ]


_index: StationIndex | None = None
_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """Return the process-wide station index, loading it from the database on first use.

    An empty result is not cached, so data loaded after start-up is picked up.
    """
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                index = _load_from_database()
                if not len(index):
                    return index
                _index = index
    return _index


def reset_station_index() -> None:
    global _index
    with _lock:
        _index = None


def _load_from_database() -> StationIndex:
    from fuel_planner.models import FuelStation

    rows = FuelStation.objects.order_by("opis_id").values_list(
        "opis_id", "name", "address", "city", "state", "retail_price", "latitude", "longitude"
    )
    return StationIndex(
        [
            Station(opis_id, name, address, city, state, float(price), lat, lon)
            for opis_id, name, address, city, state, price, lat, lon in rows
        ]
    )
