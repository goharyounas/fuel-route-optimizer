from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price list, with coordinates resolved at city level."""

    opis_id = models.PositiveIntegerField(unique=True, help_text="OPIS truck stop identifier.")
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(
        max_digits=10,
        decimal_places=8,
        help_text="USD per gallon. Lowest price listed for the station in the source file.",
    )
    price_observations = models.PositiveSmallIntegerField(
        default=1, help_text="Number of price rows the source file lists for this station."
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    location_source = models.CharField(
        max_length=32, help_text="Where the coordinates came from (e.g. census_place, geonames)."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price:.3f}"
