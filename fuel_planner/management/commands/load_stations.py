"""Load the OPIS fuel price list into the database.

    python manage.py load_stations

The price list has one row per price observation. 678 truck stops appear more than
once (same OPIS id, a different price and sometimes a different spelling of the name),
so rows are grouped by OPIS id and the lowest listed price is kept, since that's
the best price a driver can get there. Coordinates come from data/station_locations.csv
(see build_geodata).
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel_planner.models import FuelStation


class Command(BaseCommand):
    help = "Load fuel stations and prices from the CSV price list (replaces existing rows)."

    def add_arguments(self, parser):
        config = settings.FUEL_PLANNER
        parser.add_argument("--prices", type=Path, default=config["FUEL_PRICES_CSV"])
        parser.add_argument("--locations", type=Path, default=config["STATION_LOCATIONS_CSV"])

    def handle(self, *args, prices: Path, locations: Path, **options):
        for path in (prices, locations):
            if not path.exists():
                raise CommandError(f"File not found: {path}")
        coordinates = self._read_locations(locations)
        rows_by_station, skipped_rows = self._read_prices(prices)

        stations, unlocated = [], []
        for opis_id, rows in rows_by_station.items():
            city, state = rows[0]["City"].strip(), rows[0]["State"].strip()
            location = coordinates.get((city, state))
            if location is None:
                unlocated.append(f"{opis_id} ({city}, {state})")
                continue
            names = Counter(row["Truckstop Name"].strip() for row in rows)
            stations.append(
                FuelStation(
                    opis_id=opis_id,
                    name=max(names, key=lambda name: (names[name], len(name))),
                    address=rows[0]["Address"].strip(),
                    city=city,
                    state=state,
                    rack_id=int(rows[0]["Rack ID"]) if rows[0]["Rack ID"].strip().isdigit() else None,
                    retail_price=min(row["price"] for row in rows),
                    price_observations=len(rows),
                    latitude=location[0],
                    longitude=location[1],
                    location_source=location[2],
                )
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)

        duplicates = sum(1 for rows in rows_by_station.values() if len(rows) > 1)
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(stations)} stations from {sum(map(len, rows_by_station.values()))} price rows "
                f"({duplicates} stations listed more than once; kept their lowest price)."
            )
        )
        if skipped_rows:
            self.stdout.write(self.style.WARNING(f"Skipped {skipped_rows} rows with an invalid id or price."))
        if unlocated:
            self.stdout.write(
                self.style.WARNING(f"{len(unlocated)} stations have no coordinates: {unlocated[:10]}")
            )
        self.stdout.write("Restart running servers to pick up the new data.")

    @staticmethod
    def _read_locations(path: Path) -> dict[tuple[str, str], tuple[float, float, str]]:
        with open(path, newline="", encoding="utf-8") as handle:
            return {
                (row["city"], row["state"]): (float(row["latitude"]), float(row["longitude"]), row["source"])
                for row in csv.DictReader(handle)
            }

    @staticmethod
    def _read_prices(path: Path) -> tuple[dict[int, list[dict]], int]:
        grouped: dict[int, list[dict]] = defaultdict(list)
        skipped = 0
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = Decimal(row["Retail Price"].strip())
                except (ValueError, InvalidOperation, KeyError, AttributeError):
                    skipped += 1
                    continue
                if price <= 0:
                    skipped += 1
                    continue
                grouped[opis_id].append({**row, "price": price})
        return grouped, skipped
