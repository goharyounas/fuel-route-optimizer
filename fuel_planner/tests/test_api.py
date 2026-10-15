import numpy as np
import pytest

from fuel_planner.models import FuelStation
from fuel_planner.services.errors import RouteNotFoundError, UpstreamServiceError
from fuel_planner.services.geo import encode_polyline
from fuel_planner.services.routing import RouteData

pytestmark = pytest.mark.django_db

URL = "/api/v1/route/"
CHICAGO_DALLAS = {"start": "Chicago, IL", "finish": "Dallas, TX"}


def test_plan_between_two_cities(client, osrm):
    response = client.get(URL, CHICAGO_DALLAS)
    assert response.status_code == 200
    body = response.json()

    assert body["start"]["label"] == "Chicago, IL" and body["finish"]["label"] == "Dallas, TX"
    assert body["route"]["distance_miles"] == pytest.approx(961.5, abs=0.1)
    stops, summary = body["fuel_stops"], body["summary"]
    assert summary["number_of_stops"] == len(stops) >= 2
    # Money adds up to the cent; the trip buys exactly the fuel it burns (961.5 mi / 10 mpg).
    assert summary["total_fuel_cost"] == pytest.approx(sum(s["cost"] for s in stops), abs=0.001)
    assert summary["total_gallons_purchased"] == pytest.approx(summary["fuel_burned_gallons"], abs=0.001)
    assert summary["fuel_burned_gallons"] == pytest.approx(body["route"]["distance_miles"] / 10, abs=0.01)
    # Never more than a tank between stops, and never more than a tank in the tank.
    miles = [0, *(s["route_mile"] for s in stops), body["route"]["distance_miles"]]
    assert max(np.diff(miles)) <= 500
    assert all(s["fuel_on_arrival_gallons"] + s["gallons"] <= 50.001 for s in stops)

    kinds = [f["properties"]["kind"] for f in body["map"]["geojson"]["features"]]
    assert kinds == ["route", "start", "finish", *["fuel_stop"] * len(stops)]
    assert body["map"]["url"].startswith("http://testserver/api/v1/route/map/?start=Chicago")
    assert body["meta"]["external_api_calls"] == {"routing": 1, "geocoding": 0}


def test_repeated_request_is_served_from_cache_without_calling_osrm(client, osrm):
    first = client.get(URL, CHICAGO_DALLAS).json()
    second = client.get(URL, CHICAGO_DALLAS).json()
    assert len(osrm.calls) == 1
    assert second["meta"]["cache"]["plan"] == "hit"
    assert second["meta"]["external_api_calls"] == {"routing": 0, "geocoding": 0}
    assert second["fuel_stops"] == first["fuel_stops"]


def test_changing_stop_cost_reuses_the_cached_route(client, osrm):
    practical = client.get(URL, CHICAGO_DALLAS).json()
    cheapest = client.get(URL, {**CHICAGO_DALLAS, "stop_cost": 0}).json()
    assert len(osrm.calls) == 1
    assert cheapest["meta"]["cache"] == {"plan": "miss", "route": "hit"}
    assert cheapest["summary"]["total_fuel_cost"] <= practical["summary"]["total_fuel_cost"]
    assert cheapest["summary"]["number_of_stops"] > practical["summary"]["number_of_stops"]


def test_post_json_with_coordinate_objects(client, osrm):
    response = client.post(
        URL,
        {"start": {"lat": 41.837045, "lng": -87.684939}, "finish": "32.793333,-96.766513"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["start"]["source"] == "coordinates"


@pytest.mark.parametrize(
    "params, field",
    [({"start": "Chicago, IL"}, "finish"), ({"start": "", "finish": "Dallas, TX"}, "start"),
     ({**CHICAGO_DALLAS, "stop_cost": -1}, "stop_cost")],
)  # fmt: skip
def test_validation_errors(client, params, field):
    response = client.get(URL, params)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request" and field in error["details"]


def test_non_us_location_is_rejected(client):
    response = client.get(URL, {"start": "43.6532,-79.3832", "finish": "Dallas, TX"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "outside_usa"


def test_same_start_and_finish_is_rejected(client):
    response = client.get(URL, {"start": "Dallas, TX", "finish": "dallas, texas"})
    assert response.json()["error"]["code"] == "same_location"


def test_unknown_location(client, nominatim):
    response = client.get(URL, {"start": "Qwertyuiop Zxcv", "finish": "Dallas, TX"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "location_not_found"


def test_route_without_stations_longer_than_range_is_unprocessable(client, osrm):
    pacific = np.column_stack((np.full(20, 35.0), np.linspace(-130, -143, 20)))
    osrm.result = RouteData(700 * 1609.344, 40000, encode_polyline(pacific))
    response = client.get(URL, CHICAGO_DALLAS)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "no_feasible_fuel_plan"
    assert error["details"]["stations_on_route"] == 0


def test_assumption_text_follows_the_plan():
    from fuel_planner.services.optimizer import FuelPlan
    from fuel_planner.services.planner import RoutePlanner

    neutral = FuelPlan((), 900, start_fuel_gallons=5, end_fuel_gallons=5, fuel_burned_gallons=90)
    reduced = FuelPlan((), 480, start_fuel_gallons=5, end_fuel_gallons=2, fuel_burned_gallons=48)
    assert "finishes with the same amount" in RoutePlanner._fuel_assumption(neutral, 5)
    assert "arrives with 2.0 gallons" in RoutePlanner._fuel_assumption(reduced, 5)


def test_no_drivable_route(client, osrm):
    osrm.result = RouteNotFoundError("No drivable route connects these locations.")
    response = client.get(URL, {"start": "21.3069,-157.8583", "finish": "Dallas, TX"})  # Honolulu
    assert (response.status_code, response.json()["error"]["code"]) == (422, "route_not_found")


def test_routing_outage_is_a_503(client, osrm):
    osrm.result = UpstreamServiceError("The routing service timed out.")
    response = client.get(URL, CHICAGO_DALLAS)
    assert (response.status_code, response.json()["error"]["code"]) == (503, "upstream_unavailable")


def test_missing_station_data_is_a_503(client, osrm):
    FuelStation.objects.all().delete()
    response = client.get(URL, CHICAGO_DALLAS)
    assert (response.status_code, response.json()["error"]["code"]) == (503, "station_data_missing")


def test_html_map(client, osrm):
    response = client.get("/api/v1/route/map/", CHICAGO_DALLAS)
    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="plan-data"' in html and 'id="stations-data"' in html and "leaflet" in html
    assert "QUIKTRIP" in html


def test_html_map_shows_errors_and_empty_form(client):
    assert client.get("/").status_code == 200
    response = client.get(
        "/api/v1/route/map/", {"start": "43.6532,-79.3832", "finish": "Dallas, TX", "stop_cost": ""}
    )
    assert response.status_code == 400
    assert "outside the USA" in response.content.decode()


def test_post_without_trailing_slash(client, osrm):
    response = client.post("/api/v1/route", CHICAGO_DALLAS, content_type="application/json")
    assert response.status_code == 200


def test_health(client):
    body = client.get("/api/v1/health/").json()
    assert body["status"] == "ok" and body["stations_loaded"] == 6738
