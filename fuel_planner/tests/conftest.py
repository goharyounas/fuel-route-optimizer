import json
from pathlib import Path

import pytest
import requests
from django.core.cache import cache
from django.core.management import call_command

from fuel_planner.services.geocoding import NominatimClient
from fuel_planner.services.planner import get_planner
from fuel_planner.services.routing import OsrmClient, RouteData
from fuel_planner.services.stations import reset_station_index

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Load the real price list once for the whole test session."""
    with django_db_blocker.unblock():
        call_command("load_stations", verbosity=0)


@pytest.fixture(autouse=True)
def isolated_state(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}
    cache.clear()
    get_planner.cache_clear()
    reset_station_index()
    yield
    cache.clear()
    get_planner.cache_clear()
    reset_station_index()


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Fail loudly on any real HTTP call; only tests marked ``live`` may reach OSRM / Nominatim."""
    if request.node.get_closest_marker("live"):
        return

    def refuse(self, prepared, **kwargs):
        raise RuntimeError(f"Unmocked HTTP request in a test: {prepared.method} {prepared.url}")

    monkeypatch.setattr(requests.Session, "send", refuse)


def recorded_route(name: str = "osrm_chicago_dallas.json") -> RouteData:
    route = json.loads((FIXTURES / name).read_text())["routes"][0]
    return RouteData(route["distance"], route["duration"], route["geometry"])


class FakeRouter:
    """Stands in for OSRM: returns a canned route (or raises) and counts calls."""

    def __init__(self):
        self.calls = []
        self.result = recorded_route()

    def __call__(self, client, start, finish):
        self.calls.append((start, finish))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, 1


@pytest.fixture
def osrm(monkeypatch):
    router = FakeRouter()
    monkeypatch.setattr(OsrmClient, "route", lambda self, start, finish: router(self, start, finish))
    return router


@pytest.fixture
def nominatim(monkeypatch):
    """Free-text geocoder stub: map of query -> (lat, lon, label) or None."""
    answers, calls = {}, []

    def search(self, query):
        calls.append(query)
        return answers.get(query)

    monkeypatch.setattr(NominatimClient, "search", search)
    return answers, calls
