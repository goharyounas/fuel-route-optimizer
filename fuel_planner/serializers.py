import math

from rest_framework import serializers


class LocationField(serializers.Field):
    """A US location as free text ("Chicago, IL", an address, "41.88,-87.63") or {"lat": .., "lng": ..}."""

    default_error_messages = {
        "invalid": (
            'Expected "City, ST", an address, "lat,lng" text, or an object like '
            '{"lat": 41.88, "lng": -87.63}.'
        ),
        "blank": "This field may not be blank.",
        "too_long": "Locations are limited to 200 characters.",
    }

    def to_internal_value(self, data):
        if isinstance(data, str):
            value = " ".join(data.split())
            if not value:
                self.fail("blank")
            if len(value) > 200:
                self.fail("too_long")
            return value
        if isinstance(data, dict):
            lat = data.get("lat", data.get("latitude"))
            lng = data.get("lng", data.get("lon", data.get("longitude")))
            try:
                lat, lng = float(lat), float(lng)
            except (TypeError, ValueError):
                self.fail("invalid")
            if not (math.isfinite(lat) and math.isfinite(lng)):
                self.fail("invalid")
            return {"lat": lat, "lng": lng}
        self.fail("invalid")

    def to_representation(self, value):
        return value


class RoutePlanRequestSerializer(serializers.Serializer):
    start = LocationField(help_text='Where the trip starts, e.g. "Chicago, IL".')
    finish = LocationField(help_text='Where the trip ends, e.g. "Dallas, TX".')
    stop_cost = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1000,
        help_text="USD charged per fuel stop when choosing stops. 0 = absolute cheapest fuel.",
    )


def location_as_text(value) -> str:
    """Render a validated location back to query-string form."""
    return f"{value['lat']},{value['lng']}" if isinstance(value, dict) else value
