import pytest
from django.core.cache import cache

from fuel_planner.services.errors import LocationError
from fuel_planner.services.planner import get_planner


def resolve(value):
    return get_planner().geocoder.resolve(value, field="start")


def test_coordinates_text_needs_no_lookup(nominatim):
    location, calls = resolve("41.8781, -87.6298")
    assert (location.latitude, location.longitude, location.source, calls) == (
        41.8781,
        -87.6298,
        "coordinates",
        0,
    )
    assert nominatim[1] == []


def test_coordinates_object():
    location, calls = resolve({"lat": 32.7767, "lng": -96.797})
    assert location.source == "coordinates" and calls == 0


def test_city_state_comes_from_the_offline_gazetteer(nominatim):
    location, calls = resolve("Dallas, TX")
    assert location.source == "gazetteer" and calls == 0
    assert nominatim[1] == []


def test_free_text_uses_nominatim_once_then_the_cache(nominatim):
    answers, calls = nominatim
    answers["1600 Pennsylvania Ave NW, Washington, DC"] = (38.8977, -77.0365, "White House")
    first, first_calls = resolve("1600 Pennsylvania Ave NW, Washington, DC")
    second, second_calls = resolve("1600  pennsylvania ave nw, washington, dc")
    assert first.source == "nominatim" and first_calls == 1
    assert (second.latitude, second_calls) == (38.8977, 0)
    assert len(calls) == 1


def test_unknown_place_is_reported_and_negative_result_cached(nominatim):
    with pytest.raises(LocationError) as error:
        resolve("Nowhere Special Place 123")
    assert error.value.code == "location_not_found"
    with pytest.raises(LocationError):
        resolve("Nowhere Special Place 123")
    assert len(nominatim[1]) == 1


@pytest.mark.parametrize("value", ["43.6532,-79.3832", "-87.6298,41.8781", {"lat": 19.43, "lng": -99.13}])
def test_points_outside_the_usa_are_rejected(value):
    with pytest.raises(LocationError) as error:
        resolve(value)
    assert error.value.code == "outside_usa"


@pytest.mark.parametrize("query", ["Toronto, ON", "Vancouver, British Columbia", "Monterrey, Mexico"])
def test_queries_naming_another_country_are_rejected_without_a_lookup(nominatim, query):
    with pytest.raises(LocationError) as error:
        resolve(query)
    assert error.value.code == "outside_usa"
    assert nominatim[1] == []


def test_invalid_coordinates_are_rejected():
    with pytest.raises(LocationError) as error:
        resolve("123.0, 45.0")
    assert error.value.code == "invalid_location"


def test_cache_keys_are_memcached_safe(nominatim):
    nominatim[0]["Times Square New York"] = (40.758, -73.9855, "Times Square")
    resolve("Times Square New York")
    keys = list(cache._cache.keys())
    assert keys and all(" " not in key for key in keys)


def test_geocoded_place_outside_the_usa_is_cached_too(nominatim):
    nominatim[0]["Some Text Somewhere"] = (43.6532, -79.3832, "Toronto")
    for _ in range(2):
        with pytest.raises(LocationError) as error:
            resolve("Some Text Somewhere")
        assert error.value.code == "outside_usa"
    assert len(nominatim[1]) == 1


def test_invalid_nominatim_response_is_an_upstream_error(monkeypatch):
    from fuel_planner.services.errors import UpstreamServiceError
    from fuel_planner.services.geocoding import NominatimClient

    class Broken:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    client = NominatimClient("https://nominatim.test", timeout=5, user_agent="test")
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: Broken())
    monkeypatch.setattr("fuel_planner.services.geocoding.time.sleep", lambda seconds: None)
    with pytest.raises(UpstreamServiceError):
        client.search("anything")
