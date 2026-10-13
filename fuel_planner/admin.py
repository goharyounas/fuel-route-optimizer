from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "retail_price", "location_source")
    list_filter = ("state", "location_source")
    search_fields = ("name", "city", "address", "opis_id")
    ordering = ("state", "city", "name")
