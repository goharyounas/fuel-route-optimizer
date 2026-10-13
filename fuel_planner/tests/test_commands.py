from decimal import Decimal

import pytest
from django.core.management import call_command

from fuel_planner.management.commands.build_geodata import Candidate, Command
from fuel_planner.models import FuelStation

PRICES = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.799
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
8,NOWHERE STOP,US-1,Atlantis,ZZ,1,3.1
9,BAD PRICE,US-1,Big Cabin,OK,307,n/a
"""
LOCATIONS = """city,state,latitude,longitude,source
Gila Bend,AZ,32.95,-112.72,census_place
Big Cabin,OK,36.54,-95.22,census_place
"""


@pytest.mark.django_db
def test_load_stations_groups_by_opis_id_and_keeps_the_lowest_price(tmp_path, capsys):
    prices, locations = tmp_path / "prices.csv", tmp_path / "locations.csv"
    prices.write_text(PRICES)
    locations.write_text(LOCATIONS)
    call_command("load_stations", prices=prices, locations=locations)

    assert FuelStation.objects.count() == 2
    pilot = FuelStation.objects.get(opis_id=20)
    assert pilot.retail_price == Decimal("3.799")
    assert pilot.price_observations == 2
    assert pilot.name == "PILOT TRAVEL CENTER #1243"  # the most descriptive spelling
    output = capsys.readouterr().out
    assert "Skipped 1 rows" in output and "1 stations have no coordinates" in output


def test_ambiguous_city_names_resolve_to_the_candidate_near_the_same_rack():
    station_cities = {("Nashville", "TN"): ["US:460"], ("Antioch", "TN"): ["US:460"]}
    nashville = Candidate("Nashville", 36.16, -86.78, "census_place", 1)
    antiochs = [
        Candidate("Antioch", 35.79, -88.97, "geonames", 0),  # rural West Tennessee
        Candidate("Antioch", 36.06, -86.67, "geonames", 0),  # the Nashville suburb
    ]
    indexes = [("census_place", {("TN", "nashville"): [nashville], ("TN", "antioch"): antiochs})]
    command = Command()
    resolved, unmatched = command._match(station_cities, indexes, overrides={})
    assert unmatched == []
    assert resolved[("Antioch", "TN")] == antiochs[1]
