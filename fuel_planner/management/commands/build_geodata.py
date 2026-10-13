"""Derive the geodata files the API needs from public sources.

The fuel price list has no coordinates, only a city and state for each truck stop.
This command resolves every city to coordinates without a geocoding API, by matching
names against official gazetteers. It also produces the lookup files used to validate
user input.

    python manage.py build_geodata            # offline matching + data/geocode_overrides.csv
    python manage.py build_geodata --online   # also ask Nominatim about anything still unmatched

Outputs (committed to the repository, so the API runs without this step):
    data/station_locations.csv   city/state of every station -> latitude/longitude
    data/us_places.csv           US Census places, for offline "City, ST" input
    data/us_boundary.geojson     USA outline, to check that coordinate input is in the US

Sources (cached in data/raw/, which is git-ignored):
    US Census Bureau 2026 Gazetteer: places and county subdivisions (public domain)
    GeoNames US and CA populated places (CC BY 4.0)
    Natural Earth 1:10m admin-0 countries (public domain)
"""

from __future__ import annotations

import csv
import json
import statistics
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel_planner.services.gazetteer import US_STATES, normalize_place_name
from fuel_planner.services.geo import haversine_miles

_CENSUS_GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer"
SOURCES = {
    "census_place": (f"{_CENSUS_GAZETTEER}/2026_Gaz_place_national.zip", "2026_Gaz_place_national.txt"),
    "census_cousub": (f"{_CENSUS_GAZETTEER}/2026_Gaz_cousubs_national.zip", "2026_Gaz_cousubs_national.txt"),
    "geonames_us": ("https://download.geonames.org/export/dump/US.zip", "US.txt"),
    "geonames_ca": ("https://download.geonames.org/export/dump/CA.zip", "CA.txt"),
    "natural_earth": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
        "ne_10m_admin_0_countries.geojson",
        None,
    ),
}
# GeoNames encodes Canadian provinces as numeric admin1 codes.
CANADIAN_PROVINCES = {
    "AB": "01", "BC": "02", "MB": "03", "NB": "04", "NL": "05", "NS": "07", "ON": "08",
    "PE": "09", "QC": "10", "SK": "11", "YT": "12", "NT": "13", "NU": "14",
}  # fmt: skip
# Census names carry their legal/statistical type ("Tomah city", "Abanda CDP").
CENSUS_NAME_SUFFIXES = sorted(
    [
        " city and borough",
        " consolidated government (balance)",
        " unified government (balance)",
        " metro government (balance)",
        " metropolitan government (balance)",
        " urban county",
        " municipality",
        " borough",
        " city",
        " town",
        " village",
        " cdp",
        " comunidad",
        " zona urbana",
        " charter township",
        " township",
        " ccd",
        " plantation",
        " gore",
        " grant",
        " location",
        " purchase",
        " reservation",
        " unorganized territory",
        " precinct",
        " district",
    ],  # fmt: skip
    key=len,
    reverse=True,
)
EXCLUDED_GEONAMES_CODES = {"PPLQ", "PPLH", "PPLW"}  # abandoned, historical, destroyed


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    latitude: float
    longitude: float
    source: str
    rank: float  # tie-breaker when nothing else distinguishes same-name places


def strip_census_suffix(name: str) -> str:
    name = name.removesuffix(" (balance)")
    lowered = name.lower()
    for suffix in CENSUS_NAME_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def census_names(raw_name: str) -> list[str]:
    """Names a Census place is known by. Consolidated city-counties such as
    "Nashville-Davidson metropolitan government (balance)" also answer to the city name."""
    name = strip_census_suffix(raw_name)
    consolidated = raw_name.endswith("(balance)") or raw_name.endswith("urban county")
    short = name.replace("/", "-").split("-")[0].strip()
    return [name, short] if consolidated and short != name else [name]


class Command(BaseCommand):
    help = "Resolve station cities to coordinates and build the offline gazetteer files."

    def add_arguments(self, parser):
        parser.add_argument("--raw-dir", type=Path, default=settings.DATA_DIR / "raw")
        parser.add_argument(
            "--online", action="store_true", help="Query Nominatim (1 request/second) for unmatched cities."
        )

    def handle(self, *args, raw_dir: Path, online: bool, **options):
        config = settings.FUEL_PLANNER
        paths = {name: self._ensure_source(raw_dir, name) for name in SOURCES}

        station_cities = self._load_station_cities(config["FUEL_PRICES_CSV"])
        self.stdout.write(f"Price list: {len(station_cities)} distinct city/state pairs")

        census_places = self._load_census(paths["census_place"], "census_place")
        indexes = [
            ("census_place", census_places),
            ("geonames", self._load_geonames(paths["geonames_us"], paths["geonames_ca"])),
            ("census_cousub", self._load_census(paths["census_cousub"], "census_cousub")),
        ]
        overrides = self._load_overrides(settings.DATA_DIR / "geocode_overrides.csv")

        resolved, unmatched = self._match(station_cities, indexes, overrides)
        if unmatched and online:
            resolved.update(self._nominatim_fallback(unmatched, config))
            unmatched = [key for key in unmatched if key not in resolved]
        self._report(station_cities, resolved, unmatched)

        self._write_station_locations(config["STATION_LOCATIONS_CSV"], resolved)
        self._write_places(config["PLACES_CSV"], census_places)
        self._write_boundary(config["US_BOUNDARY_GEOJSON"], paths["natural_earth"])
        self.stdout.write(self.style.SUCCESS("Geodata written to data/."))

    # Sources ------------------------------------------------------------------------

    def _ensure_source(self, raw_dir: Path, name: str) -> Path:
        url, member = SOURCES[name]
        folder = raw_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / url.rsplit("/", 1)[-1]
        if not archive.exists():
            self.stdout.write(f"Downloading {url}")
            partial = archive.with_suffix(archive.suffix + ".part")
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        handle.write(chunk)
            partial.rename(archive)
        if member is None:
            return archive
        extracted = folder / member
        if not extracted.exists():
            with zipfile.ZipFile(archive) as bundle:
                bundle.extract(member, folder)
        return extracted

    def _load_station_cities(self, path: Path) -> dict[tuple[str, str], list[str]]:
        """(city, state) -> rack key of every price row in that city (one entry per row).

        Rack ids are numbered separately in each country (rack 50 is both Calgary and
        Columbus, GA), so the key includes the country.
        """
        cities: dict[tuple[str, str], list[str]] = defaultdict(list)
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                state = row["State"].strip()
                country = "CA" if state in CANADIAN_PROVINCES else "US"
                cities[(row["City"].strip(), state)].append(f"{country}:{row['Rack ID'].strip()}")
        return cities

    def _load_census(self, path: Path, source: str) -> dict[tuple[str, str], list[Candidate]]:
        index: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
        with open(path, newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle, delimiter="|"):
                row = {key.strip(): value.strip() for key, value in raw.items()}
                # Prefer incorporated places (FUNCSTAT "A") over statistical ones, then larger ones.
                rank = (1e13 if row.get("FUNCSTAT") == "A" else 0.0) + float(row["ALAND"])
                for name in census_names(row["NAME"]):
                    candidate = Candidate(name, float(row["INTPTLAT"]), float(row["INTPTLONG"]), source, rank)
                    index[(row["USPS"], normalize_place_name(name))].append(candidate)
        return index

    def _load_geonames(self, us_path: Path, ca_path: Path) -> dict[tuple[str, str], list[Candidate]]:
        index: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
        provinces = {code: abbr for abbr, code in CANADIAN_PROVINCES.items()}
        for path, region_of in ((us_path, lambda c: c), (ca_path, provinces.get)):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    cols = line.rstrip("\n").split("\t")
                    if cols[6] != "P" or cols[7] in EXCLUDED_GEONAMES_CODES or "(historical)" in cols[1]:
                        continue
                    region = region_of(cols[10])
                    if not region:
                        continue
                    candidate = Candidate(
                        cols[1], float(cols[4]), float(cols[5]), "geonames", float(cols[14] or 0)
                    )
                    for key in {normalize_place_name(cols[1]), normalize_place_name(cols[2])}:
                        index[(region, key)].append(candidate)
        return index

    def _load_overrides(self, path: Path) -> dict[tuple[str, str], Candidate]:
        if not path.exists():
            return {}
        with open(path, newline="", encoding="utf-8") as handle:
            return {
                (row["city"].strip(), row["state"].strip()): Candidate(
                    row["city"], float(row["latitude"]), float(row["longitude"]), "override", 0.0
                )
                for row in csv.DictReader(handle)
            }

    # Matching -----------------------------------------------------------------------

    def _match(self, station_cities, indexes, overrides):
        resolved: dict[tuple[str, str], Candidate] = {}
        ambiguous: dict[tuple[str, str], list[Candidate]] = {}
        unmatched: list[tuple[str, str]] = []
        for key in station_cities:
            if key in overrides:
                resolved[key] = overrides[key]
                continue
            city, state = key
            lookup = (state, normalize_place_name(city))
            candidates = next((index[lookup] for _, index in indexes if lookup in index), None)
            if not candidates:
                unmatched.append(key)
            elif len(candidates) == 1:
                resolved[key] = candidates[0]
            else:
                ambiguous[key] = candidates

        # Same-name places in one state (e.g. six "Antioch"s in Tennessee): stations that share
        # an OPIS rack (wholesale market) are near each other, so pick the candidate closest to
        # the already-resolved stations of the same racks.
        rack_points: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for key, candidate in resolved.items():
            for rack in station_cities[key]:
                rack_points[rack].append((candidate.latitude, candidate.longitude))
        centroids = {
            rack: (statistics.median(p[0] for p in points), statistics.median(p[1] for p in points))
            for rack, points in rack_points.items()
        }
        for key, candidates in ambiguous.items():
            racks = [rack for rack in station_cities[key] if rack in centroids]
            if racks:
                resolved[key] = min(
                    candidates,
                    key=lambda c: sum(
                        float(haversine_miles(c.latitude, c.longitude, *centroids[r])) for r in racks
                    ),
                )
            else:
                resolved[key] = max(candidates, key=lambda c: c.rank)
        self.stdout.write(f"Disambiguated {len(ambiguous)} city names shared by several places in a state")
        return resolved, unmatched

    def _nominatim_fallback(self, unmatched, config):
        session = requests.Session()
        session.headers["User-Agent"] = config["HTTP_USER_AGENT"]
        found = {}
        for city, state in unmatched:
            country = "ca" if state in CANADIAN_PROVINCES else "us"
            response = session.get(
                f"{config['NOMINATIM_BASE_URL']}/search",
                params={"q": f"{city}, {state}", "format": "jsonv2", "limit": 1, "countrycodes": country},
                timeout=config["HTTP_TIMEOUT_SECONDS"],
            )
            time.sleep(1.1)  # Nominatim usage policy: at most one request per second
            if response.ok and response.json():
                hit = response.json()[0]
                found[(city, state)] = Candidate(city, float(hit["lat"]), float(hit["lon"]), "nominatim", 0.0)
        return found

    def _report(self, station_cities, resolved, unmatched):
        sources = Counter(candidate.source for candidate in resolved.values())
        self.stdout.write(f"Resolved {len(resolved)}/{len(station_cities)} cities: {dict(sources)}")
        for city, state in unmatched:
            self.stdout.write(self.style.WARNING(f"Unmatched: {city}, {state}"))
        if unmatched:
            raise CommandError(
                f"{len(unmatched)} cities could not be located. Add them to data/geocode_overrides.csv "
                "or re-run with --online."
            )

    # Outputs ------------------------------------------------------------------------

    def _write_station_locations(self, path: Path, resolved: dict[tuple[str, str], Candidate]):
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["city", "state", "latitude", "longitude", "source"])
            for (city, state), c in sorted(resolved.items(), key=lambda item: (item[0][1], item[0][0])):
                writer.writerow([city, state, f"{c.latitude:.6f}", f"{c.longitude:.6f}", c.source])
        self.stdout.write(f"Wrote {path.name}: {len(resolved)} cities")

    def _write_places(self, path: Path, census_places: dict[tuple[str, str], list[Candidate]]):
        rows = []
        for (state, _), candidates in census_places.items():
            if state in US_STATES:  # excludes Puerto Rico, which has no road link to the mainland
                best = max(candidates, key=lambda c: c.rank)
                rows.append((state, best.name, best.latitude, best.longitude))
        rows.sort()
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name", "state", "latitude", "longitude"])
            for state, name, lat, lon in rows:
                writer.writerow([name, state, f"{lat:.6f}", f"{lon:.6f}"])
        self.stdout.write(f"Wrote {path.name}: {len(rows)} places")

    def _write_boundary(self, path: Path, countries_geojson: Path):
        countries = json.loads(countries_geojson.read_text(encoding="utf-8"))
        usa = next(f for f in countries["features"] if f["properties"].get("ADM0_A3") == "USA")
        geometry = usa["geometry"]
        polygons = (
            geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        )
        rounded = [
            [[[round(x, 4), round(y, 4)] for x, y in ring] for ring in polygon] for polygon in polygons
        ]
        path.write_text(json.dumps({"type": "MultiPolygon", "coordinates": rounded}, separators=(",", ":")))
        self.stdout.write(f"Wrote {path.name}: {len(rounded)} polygons")
