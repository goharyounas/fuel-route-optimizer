import numpy as np
import pytest

from fuel_planner.services.geo import haversine_miles
from fuel_planner.services.stations import Station, StationIndex, get_station_index

# A straight east-west route along the 40th parallel, ~265 miles long.
ROUTE_LAT = np.full(50, 40.0)
ROUTE_LON = np.linspace(-100.0, -95.0, 50)
ROUTE_MILES = float(haversine_miles(40.0, -100.0, 40.0, -95.0))


def station(opis_id, lat, lon, price=3.0):
    return Station(opis_id, f"Station {opis_id}", "", "Town", "KS", price, lat, lon)


def test_matches_stations_inside_the_corridor_with_their_mile_marker():
    index = StationIndex(
        [
            station(1, 40.0, -98.0),  # on the route
            station(2, 40.04, -97.0),  # ~2.8 miles north
            station(3, 40.12, -97.0),  # ~8.3 miles north: outside a 5-mile corridor
            station(4, 35.0, -90.0),  # far away
        ]
    )
    matches = {m.index: m for m in index.match_route(ROUTE_LAT, ROUTE_LON, ROUTE_MILES, corridor_miles=5,
                                                     spacing_miles=0.5)}  # fmt: skip
    assert set(matches) == {0, 1}
    assert matches[0].route_mile == pytest.approx(float(haversine_miles(40, -100, 40, -98)), abs=0.5)
    assert matches[0].off_route_miles == pytest.approx(0, abs=0.3)
    assert matches[1].off_route_miles == pytest.approx(2.76, abs=0.3)


def test_mile_markers_are_scaled_to_the_routing_engines_distance():
    index = StationIndex([station(1, 40.0, -95.0)])
    [match] = index.match_route(ROUTE_LAT, ROUTE_LON, ROUTE_MILES * 1.1, corridor_miles=5, spacing_miles=0.5)
    assert match.route_mile == pytest.approx(ROUTE_MILES * 1.1, abs=0.1)


def test_empty_index_and_empty_route():
    assert (
        StationIndex([]).match_route(ROUTE_LAT, ROUTE_LON, ROUTE_MILES, corridor_miles=5, spacing_miles=0.5)
        == []
    )
    index = StationIndex([station(1, 40, -98)])
    assert index.match_route(np.empty(0), np.empty(0), 0, corridor_miles=5, spacing_miles=0.5) == []


def test_version_changes_with_prices():
    assert (
        StationIndex([station(1, 40, -98, 3.0)]).version != StationIndex([station(1, 40, -98, 3.1)]).version
    )


@pytest.mark.django_db
def test_index_loads_all_stations_from_the_database():
    index = get_station_index()
    assert len(index) == 6738
    assert get_station_index() is index  # cached for the process
