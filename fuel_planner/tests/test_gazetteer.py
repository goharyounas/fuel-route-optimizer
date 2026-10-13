import pytest

from fuel_planner.management.commands.build_geodata import strip_census_suffix
from fuel_planner.services.gazetteer import get_gazetteer, get_us_boundary, normalize_place_name


@pytest.mark.parametrize(
    "a, b",
    [
        ("Mc Calla", "McCalla"),
        ("Saint Johns", "St. Johns"),
        ("Mount Airy", "Mt Airy"),
        ("Fort Myers", "Ft. Myers"),
        ("Sainte Genevieve", "Ste. Genevieve"),
        ("S Coffeyville", "South Coffeyville"),
        ("De Forest", "DeForest"),
        ("Winston-Salem", "Winston Salem"),
    ],
)
def test_normalize_place_name_matches_spelling_variants(a, b):
    assert normalize_place_name(a) == normalize_place_name(b)


def test_normalize_does_not_mangle_names_containing_abbreviations():
    assert normalize_place_name("Stuart") == "stuart"
    assert normalize_place_name("Fortson") == "fortson"
    assert normalize_place_name("Mountain Home") == "mountainhome"


@pytest.mark.parametrize(
    "name, expected",
    [("Tomah city", "Tomah"), ("Abanda CDP", "Abanda"), ("Lake Havasu City city", "Lake Havasu City")],
)
def test_strip_census_suffix(name, expected):
    assert strip_census_suffix(name) == expected


@pytest.mark.parametrize(
    "query, label",
    [
        ("Chicago, IL", "Chicago, IL"),
        ("chicago, illinois", "Chicago, IL"),
        ("Chicago IL", "Chicago, IL"),
        ("Kansas City Missouri", "Kansas City, MO"),
        ("Charleston West Virginia", "Charleston, WV"),
        ("St. Louis, MO, USA", "St. Louis, MO"),
        ("Saint Louis, MO", "St. Louis, MO"),
        ("New York, NY", "New York, NY"),
        ("Nashville, TN", "Nashville, TN"),
        ("Louisville, KY", "Louisville, KY"),
        ("Indianapolis, IN", "Indianapolis, IN"),
    ],
)
def test_gazetteer_resolves_city_state_queries(query, label):
    assert get_gazetteer().resolve(query).label == label


@pytest.mark.parametrize("query", ["Atlantis, ZZ", "1600 Pennsylvania Ave NW, Washington, DC", "Chicago"])
def test_gazetteer_leaves_other_queries_unresolved(query):
    assert get_gazetteer().resolve(query) is None


@pytest.mark.parametrize(
    "lat, lon, inside",
    [
        (41.8781, -87.6298, True),  # Chicago
        (31.7619, -106.4850, True),  # El Paso, on the Mexican border
        (48.9937, -122.7471, True),  # Blaine, WA: coastal, accepted via the 1 km tolerance
        (21.3069, -157.8583, True),  # Honolulu
        (61.2181, -149.9003, True),  # Anchorage
        (31.6904, -106.4245, False),  # Ciudad Juarez
        (42.3149, -83.0364, False),  # Windsor, ON
        (43.6532, -79.3832, False),  # Toronto
        (32.5149, -117.0382, False),  # Tijuana
        (18.4655, -66.1057, False),  # San Juan, PR: no road link to the mainland
        (35.0, -130.0, False),  # Pacific Ocean
    ],
)
def test_us_boundary(lat, lon, inside):
    assert get_us_boundary().contains(lat, lon) is inside
