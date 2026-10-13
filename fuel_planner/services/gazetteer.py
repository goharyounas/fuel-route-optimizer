"""Offline place lookup ("City, ST" -> coordinates) and a USA boundary test.

Both are backed by small files in ``data/`` that ``manage.py build_geodata`` derives
from public-domain sources (US Census Gazetteer, Natural Earth), so the common
"City, ST" input never costs an external API call.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from django.conf import settings

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip
_STATE_ALIASES = {
    **{abbr.lower(): abbr for abbr in US_STATES},
    **{n.lower(): a for a, n in US_STATES.items()},
}
_COUNTRY_SUFFIXES = {"usa", "us", "u.s.", "u.s.a.", "united states", "united states of america"}
# Regions that unambiguously put a query outside the USA ("Toronto, ON").
NON_US_REGIONS = {
    "canada", "mexico", "méxico", "ab", "alberta", "bc", "british columbia", "mb", "manitoba", "nb",
    "new brunswick", "nl", "newfoundland and labrador", "ns", "nova scotia", "nt", "northwest territories",
    "nu", "nunavut", "on", "ontario", "pe", "prince edward island", "qc", "quebec", "québec", "sk",
    "saskatchewan", "yt", "yukon",
}  # fmt: skip


def names_non_us_region(query: str) -> bool:
    """True when the last comma-separated part names Canada, Mexico or a Canadian province."""
    parts = [part.strip().lower().rstrip(".") for part in query.split(",") if part.strip()]
    return len(parts) >= 2 and parts[-1] in NON_US_REGIONS


_DIRECTIONS = {"n": "north", "s": "south", "e": "east", "w": "west"}


def normalize_place_name(name: str) -> str:
    """Matching key for place names, so that e.g. "Mc Calla" == "McCalla",
    "Saint Johns" == "St. Johns", "Mt Airy" == "Mount Airy" and "S Coffeyville" ==
    "South Coffeyville"."""
    text = re.sub(r"\(.*?\)", " ", name.lower()).strip()
    text = re.sub(r"^([nsew])\.?\s+", lambda m: _DIRECTIONS[m.group(1)] + " ", text)
    text = re.sub(r"\bsainte\b|\bste\b\.?", "ste", text)
    text = re.sub(r"\bsaint\b|\bst\b\.?", "st", text)
    text = re.sub(r"\bmount\b|\bmt\b\.?", "mt", text)
    text = re.sub(r"\bfort\b|\bft\b\.?", "ft", text)
    return re.sub(r"[^a-z0-9]", "", text)


def state_code(text: str) -> str | None:
    """Two-letter code for a state abbreviation or full name, if it is one."""
    return _STATE_ALIASES.get(text.strip().lower().rstrip("."))


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    state: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


class Gazetteer:
    """In-memory index of US places keyed by (state, normalised name)."""

    def __init__(self, places: Iterable[Place]):
        self._index: dict[tuple[str, str], Place] = {}
        for place in places:
            self._index.setdefault((place.state, normalize_place_name(place.name)), place)

    @classmethod
    def from_csv(cls, path: Path) -> Gazetteer:
        with open(path, newline="", encoding="utf-8") as handle:
            return cls(
                Place(row["name"], row["state"], float(row["latitude"]), float(row["longitude"]))
                for row in csv.DictReader(handle)
            )

    def __len__(self) -> int:
        return len(self._index)

    def lookup(self, name: str, state: str) -> Place | None:
        return self._index.get((state.upper(), normalize_place_name(name)))

    def resolve(self, query: str) -> Place | None:
        """Resolve free text such as "Chicago, IL", "Chicago, Illinois" or "Kansas City MO"."""
        parts = [p.strip() for p in query.split(",") if p.strip()]
        while parts and parts[-1].lower() in _COUNTRY_SUFFIXES:
            parts.pop()
        # Exactly "City, ST": anything longer (e.g. a street address) is left to the geocoder.
        if len(parts) == 2 and (code := state_code(parts[-1])):
            return self.lookup(parts[0], code)
        if len(parts) == 1:
            words = parts[0].split()
            # Allow "City ST" / "City New York" without a comma: try 1-3 trailing words as the state.
            for size in (1, 2, 3):
                code = state_code(" ".join(words[-size:])) if len(words) > size else None
                if code and (place := self.lookup(" ".join(words[:-size]), code)):
                    return place
        return None


class UsBoundary:
    """Is a point in the USA? Tested against the Natural Earth 1:10m outline.

    The outline is generalised, so a coastal or border town's centre can land a few
    hundred metres outside it (Blaine, WA sits "in the water"). Points within
    ``tolerance_km`` of the outline are therefore accepted too. 1 km is small enough to
    reject the Mexican and Canadian cities across the border.
    """

    def __init__(self, polygons: list[list[np.ndarray]], *, tolerance_km: float = 1.0):
        # Each polygon is [outer_ring, *holes]; rings are (N, 2) arrays of (lon, lat).
        self._polygons = [(rings, rings[0].min(axis=0), rings[0].max(axis=0)) for rings in polygons]
        self.tolerance_km = tolerance_km

    @classmethod
    def from_geojson(cls, path: Path, **kwargs) -> UsBoundary:
        geometry = json.loads(Path(path).read_text(encoding="utf-8"))
        coordinates = (
            geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        )
        return cls([[np.asarray(ring, dtype=float) for ring in polygon] for polygon in coordinates], **kwargs)

    def contains(self, latitude: float, longitude: float) -> bool:
        margin = self.tolerance_km / 80.0  # degrees; generous for any US latitude
        near = [
            rings
            for rings, low, high in self._polygons
            if low[0] - margin <= longitude <= high[0] + margin
            and low[1] - margin <= latitude <= high[1] + margin
        ]
        if any(self._inside(rings, latitude, longitude) for rings in near):
            return True
        return any(self._distance_km(rings, latitude, longitude) <= self.tolerance_km for rings in near)

    @staticmethod
    def _inside(rings, latitude, longitude) -> bool:
        crossings = 0
        for ring in rings:  # even-odd rule handles holes (e.g. lakes) automatically
            x1, y1 = ring[:, 0], ring[:, 1]
            x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
            straddles = (y1 > latitude) != (y2 > latitude)
            with np.errstate(divide="ignore", invalid="ignore"):
                x_cross = x1 + (latitude - y1) * (x2 - x1) / (y2 - y1)
            crossings += int(np.count_nonzero(straddles & (longitude < x_cross)))
        return bool(crossings % 2)

    @staticmethod
    def _distance_km(rings, latitude, longitude) -> float:
        """Distance to the nearest edge, in a local equirectangular projection."""
        km_per_degree = 111.195
        scale = np.cos(np.radians(latitude))
        best = np.inf
        for ring in rings:
            x1 = (ring[:, 0] - longitude) * km_per_degree * scale
            y1 = (ring[:, 1] - latitude) * km_per_degree
            x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
            dx, dy = x2 - x1, y2 - y1
            length_sq = np.where((dx * dx + dy * dy) > 0, dx * dx + dy * dy, 1.0)
            t = np.clip(-(x1 * dx + y1 * dy) / length_sq, 0.0, 1.0)
            best = min(best, float(np.hypot(x1 + t * dx, y1 + t * dy).min()))
        return best


@lru_cache(maxsize=1)
def get_gazetteer() -> Gazetteer:
    return Gazetteer.from_csv(settings.FUEL_PLANNER["PLACES_CSV"])


@lru_cache(maxsize=1)
def get_us_boundary() -> UsBoundary:
    return UsBoundary.from_geojson(settings.FUEL_PLANNER["US_BOUNDARY_GEOJSON"])
