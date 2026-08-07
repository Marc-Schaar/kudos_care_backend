from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import Ride, RideStream


class RideStreamInline(admin.StackedInline):
    model = RideStream
    extra = 0
    fields = ["avg_headwind", "point_count"]
    readonly_fields = ["avg_headwind", "point_count"]

    @admin.display(description="Anzahl Punkte")
    def point_count(self, obj):
        if not obj or not obj.latlngs:
            return 0
        return len(obj.latlngs)


@admin.register(Ride)
class RideAdmin(GISModelAdmin):
    list_display = [
        "name",
        "athlete",
        "bike",
        "start_date",
        "distance_km",
        "elapsed_time",
    ]
    list_filter = ["bike__bike_type", "start_date"]
    search_fields = ["name", "strava_id", "athlete__strava_athlete_id"]
    date_hierarchy = "start_date"
    raw_id_fields = ["athlete", "bike"]
    readonly_fields = [
        "strava_id",
        "created_at",
        "ai_summary",
        "ai_summary_generated_at",
    ]
    inlines = [RideStreamInline]

    @admin.display(description="Distanz (km)")
    def distance_km(self, obj):
        if obj.distance is None:
            return None
        return round(obj.distance / 1000, 1)
